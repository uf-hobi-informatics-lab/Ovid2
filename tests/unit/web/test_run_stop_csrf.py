"""Regression: the run page's ■ Stop request is accepted with sign-in on.

The terminal pane intercepts the ■ Stop submit and POSTs
``/jobs/<id>/cancel`` with ``fetch``. That request carries no form body, so
it must send the CSRF token in ``X-CSRF-Token`` — read from the page's
``<body hx-headers>`` (``display/terminal/mount.js::fetchPostHeaders``).
Before the fix it sent no token, got a 403, and the job kept running.

This test reproduces the browser's exact request against the real app:
render a page, pull the token out of ``<body hx-headers>`` just as the JS
does, then POST cancel with ``X-Requested-With: fetch`` + that header.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import settings_svc

_JOB_ID = "stopjob0000000000000000000000001"


def _signed_in_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, object]:
    """Return a ``TestClient`` with an activated admin session (auth on)."""
    import importlib

    import ovid2.web.app as app_module
    import ovid2.web.settings as web_settings

    importlib.reload(web_settings)
    importlib.reload(app_module)
    settings = GlobalSettings(auth=SettingsAuth(enabled=True))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)

    from starlette.responses import Response

    from ovid2.web import db as web_db
    from ovid2.web.auth.roles import Role
    from ovid2.web.services import sessions_svc, users_svc

    cookie_mod.reset_signing_key_cache()
    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="stop-admin",
        email="stop-admin@example.org",
        name="stop-admin",
    )
    users_svc.activate(user_id=record["user_id"], role=Role.SYSTEM_ADMIN, actor="test")
    session = sessions_svc.create(
        user_id=str(record["user_id"]), idp_sid=None, absolute_ttl_min=60, idle_ttl_min=45
    )
    client = TestClient(app_module.app)
    response = Response()
    cookie_mod.issue_cookie(response, session.session_id)
    raw = next(v.decode("latin-1") for k, v in response.raw_headers if k == b"set-cookie")
    name, value = raw.split(";", 1)[0].split("=", 1)
    client.cookies.set(name, value)
    return client, web_db


def _insert_running_job(web_db: object) -> None:
    """Insert a ``running`` job row in the default workspace."""
    with web_db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, pipeline, batch_id, input_address, "
            "output_address, tables_json, transformations_json, status, instance_id, "
            "workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _JOB_ID,
                "2026-11-23T00:00:00+00:00",
                "demo",
                "demo/1",
                "in",
                "out",
                "[]",
                "[]",
                "running",
                "20261123-000000-abcdef",
                "default",
            ),
        )


def _page_token(client: TestClient) -> str:
    """Read ``X-CSRF-Token`` from ``<body hx-headers>`` like the browser JS."""
    page = client.get("/runs", follow_redirects=True)
    assert page.status_code == 200, page.status_code
    match = re.search(r"<body[^>]*hx-headers='([^']*)'", page.text)
    assert match, "base.html must expose the CSRF token on <body hx-headers>"
    return str(json.loads(html.unescape(match.group(1)))["X-CSRF-Token"])


@pytest.mark.unit
def test_background_stop_with_page_token_cancels(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ■ Stop fetch (header token from the page) flags the job for cancel."""
    client, web_db = _signed_in_client(monkeypatch)
    with client:
        _insert_running_job(web_db)
        token = _page_token(client)
        r = client.post(
            f"/jobs/{_JOB_ID}/cancel",
            headers={"X-Requested-With": "fetch", "X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        with web_db.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT cancel_requested FROM web_jobs WHERE job_id = ?", (_JOB_ID,)
            ).fetchone()
        assert row["cancel_requested"] == 1


@pytest.mark.unit
def test_background_stop_without_token_is_refused(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-fix request shape (no token) is a 403 and does not cancel."""
    client, web_db = _signed_in_client(monkeypatch)
    with client:
        _insert_running_job(web_db)
        r = client.post(
            f"/jobs/{_JOB_ID}/cancel",
            headers={"X-Requested-With": "fetch"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        with web_db.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT cancel_requested FROM web_jobs WHERE job_id = ?", (_JOB_ID,)
            ).fetchone()
        assert row["cancel_requested"] == 0


@pytest.mark.unit
def test_mount_js_sends_csrf_on_stop(src_root: Path) -> None:
    """``requestStop`` builds its headers through the CSRF-aware helper."""
    js = (src_root / "web" / "static" / "display" / "terminal" / "mount.js").read_text(
        encoding="utf-8"
    )
    body = js.split("function requestStop()", 1)[1].split("function stopFallback()", 1)[0]
    assert "fetchPostHeaders(promptForm)" in body
    assert "if (!r.ok) stopFallback()" in body
    assert 'getAttribute("hx-headers")' in js


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
