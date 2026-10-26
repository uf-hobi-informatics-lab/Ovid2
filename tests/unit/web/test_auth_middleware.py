"""Tests for the HTML auth redirect middleware (Wave 3, todo 13).

Exercises :class:`ovid2.web.auth.middleware.AuthRedirectMiddleware` through a
FastAPI ``TestClient`` built from :func:`ovid2.web.app.create_app`. The cookie
/ session setup mirrors ``test_rest_gating.py`` (a real session + user row
through ``sessions_svc`` / ``users_svc`` threaded in as a signed
``ovid2_session`` cookie); ``settings_svc.get_settings`` is monkeypatched to
toggle ``auth.enabled``.

Covered acceptance criteria:

* enabled + unauth GET ``/`` → ``302`` to ``/login?next=/``.
* enabled GET ``/login`` → ``200`` (allow-listed).
* enabled GET ``/api/v1/health`` unauth → not redirected by the middleware.
* enabled + pending user GET ``/`` → ``200`` pending page.
* enabled + active admin GET ``/`` → ``200`` dashboard.
* disabled → GET ``/`` ``200`` (no redirect, today's behaviour).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import sessions_svc, settings_svc, users_svc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` that does not auto-follow redirects."""
    client = TestClient(create_app(), follow_redirects=False)
    try:
        yield client
    finally:
        client.close()


def _enable_auth(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force ``settings_svc.get_settings`` to a known ``auth.enabled`` value."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _signed_cookie_value(session_id: str) -> str:
    """Return the signed cookie value ``cookie.issue_cookie`` would emit."""
    from http.cookies import SimpleCookie

    response = Response()
    cookie_mod.issue_cookie(response, session_id)
    line = next(
        value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"
    )
    jar: SimpleCookie[str] = SimpleCookie()
    jar.load(line)
    return jar["ovid2_session"].value


def _cookie_for_role(role: Role, subject: str) -> str:
    """Create an active user in ``role`` + session; return its signed cookie."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    record = users_svc.activate(user_id=record["user_id"], role=role, actor="test")
    session = sessions_svc.create(
        user_id=str(record["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    return _signed_cookie_value(session.session_id)


def _pending_cookie(subject: str) -> str:
    """Create a pending user (default state) + session; return its cookie."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    session = sessions_svc.create(
        user_id=str(record["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    return _signed_cookie_value(session.session_id)


def _set_cookie(client: TestClient, cookie: str | None) -> None:
    """Set (or clear) the ``ovid2_session`` cookie on the client instance."""
    client.cookies.clear()
    if cookie is not None:
        client.cookies.set("ovid2_session", cookie)


# ---------------------------------------------------------------------------
# enabled — unauthenticated redirect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enabled_unauth_root_redirects_to_login(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """enabled + no session GET / → 302 to /login?next=/."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, None)
    resp = app_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=%2F"


@pytest.mark.unit
def test_enabled_unauth_runs_redirects_with_next(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A deeper unauth page carries its own path in next."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, None)
    resp = app_client.get("/runs")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=%2Fruns"


# ---------------------------------------------------------------------------
# enabled — allow-list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enabled_login_page_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """GET /login is allow-listed → 200 (not redirected)."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, None)
    resp = app_client.get("/login")
    assert resp.status_code == 200


@pytest.mark.unit
def test_enabled_api_not_redirected_by_middleware(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """/api/v1/health unauth is NOT redirected here (route dep handles it)."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, None)
    resp = app_client.get("/api/v1/health")
    # The middleware must not issue a redirect to /login for API paths. The
    # route dependency answers JSON (401 for VIEW_PUBLIC without a session).
    assert not (resp.status_code == 302 and resp.headers.get("location", "").startswith("/login"))


# ---------------------------------------------------------------------------
# enabled — pending / active
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enabled_pending_user_sees_pending_page(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A pending user (valid session) GET / → 200 pending page, no loop."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _pending_cookie("pending-mw"))
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "Awaiting approval" in resp.text


@pytest.mark.unit
def test_enabled_disabled_user_sees_pending_page(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A disabled user keeps a role but sees the pending page (status check)."""
    _enable_auth(monkeypatch, enabled=True)
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="disabled-mw",
        email="disabled-mw@example.org",
        name="disabled-mw",
    )
    record = users_svc.activate(user_id=record["user_id"], role=Role.VIEWER, actor="test")
    users_svc.disable(user_id=record["user_id"], actor="test")
    session = sessions_svc.create(
        user_id=str(record["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    _set_cookie(app_client, _signed_cookie_value(session.session_id))
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "Awaiting approval" in resp.text


@pytest.mark.unit
def test_enabled_active_admin_sees_console(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An active admin GET / → 200 console (not redirected, not pending)."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.SYSTEM_ADMIN, "admin-mw"))
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "Awaiting approval" not in resp.text


# ---------------------------------------------------------------------------
# disabled — no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_root_open(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """auth.enabled=False → GET / is 200 with no session (today's behaviour)."""
    _enable_auth(monkeypatch, enabled=False)
    _set_cookie(app_client, None)
    resp = app_client.get("/")
    assert resp.status_code == 200


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
