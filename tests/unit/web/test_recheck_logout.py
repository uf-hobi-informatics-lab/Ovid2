"""Unit tests for the silent-recheck + front-channel logout routes (todo 12).

Exercises the two todo-12 endpoints on :mod:`ovid2.web.routes.auth`
through a FastAPI ``TestClient``:

* ``GET /auth/recheck`` returns ``{"authenticated": false}`` for a revoked
  / absent session and ``{"authenticated": true}`` for a valid one; when
  ``silent_recheck`` is disabled it always reports ``true``. It never 500s.
* ``GET /auth/frontchannel-logout`` revokes every session matching the
  ``sid`` query param and returns 200 with
  ``Cache-Control: no-cache, no-store``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.web import db as web_db
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import sessions_svc, settings_svc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean web DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


def _install_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    silent_recheck: bool = True,
) -> None:
    """Enable auth with the given silent-recheck toggle."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled, silent_recheck=silent_recheck))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _client() -> Any:
    """Build a TestClient over a freshly-created app (no auto-redirect)."""
    from fastapi.testclient import TestClient

    from ovid2.web.app import create_app

    return TestClient(create_app(), follow_redirects=False)


def _signed(session_id: str) -> str:
    """Return the signed cookie value ``cookie.issue_cookie`` would emit."""
    from http.cookies import SimpleCookie

    from starlette.responses import Response

    response = Response()
    cookie_mod.issue_cookie(response, session_id)
    line = next(
        value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"
    )
    jar: SimpleCookie[str] = SimpleCookie()
    jar.load(line)
    return jar["ovid2_session"].value


# ---------------------------------------------------------------------------
# /auth/recheck
# ---------------------------------------------------------------------------


def test_recheck_false_for_absent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cookie → authenticated:false."""
    _install_auth(monkeypatch)
    resp = _client().get("/auth/recheck")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_recheck_true_for_valid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid break-glass session → authenticated:true."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="breakglass", idp_sid=None, absolute_ttl_min=60, idle_ttl_min=60
    )
    client = _client()
    client.cookies.set("ovid2_session", _signed(session.session_id))
    resp = client.get("/auth/recheck")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True}


def test_recheck_false_for_revoked_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked session → authenticated:false."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="breakglass", idp_sid=None, absolute_ttl_min=60, idle_ttl_min=60
    )
    sessions_svc.revoke(session.session_id)
    client = _client()
    client.cookies.set("ovid2_session", _signed(session.session_id))
    resp = client.get("/auth/recheck")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_recheck_true_when_silent_recheck_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When silent_recheck is off, recheck always reports true (client no-op)."""
    _install_auth(monkeypatch, silent_recheck=False)
    resp = _client().get("/auth/recheck")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True}


def test_recheck_never_500s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if get_settings blows up, recheck fails open with true (200)."""

    def _boom() -> GlobalSettings:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_svc, "get_settings", _boom)
    resp = _client().get("/auth/recheck")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True}


# ---------------------------------------------------------------------------
# /auth/frontchannel-logout
# ---------------------------------------------------------------------------


def test_frontchannel_logout_revokes_by_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known sid revokes the matching session and returns 200 + no-store."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="user-1",
        idp_sid="idp-sid-abc",
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    assert sessions_svc.get_valid(session.session_id) is not None

    resp = _client().get(
        "/auth/frontchannel-logout",
        params={"sid": "idp-sid-abc", "iss": "https://accounts.google.com"},
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache, no-store"
    assert sessions_svc.get_valid(session.session_id) is None


def test_frontchannel_logout_unknown_sid_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown sid still returns 200 + no-store (idempotent)."""
    _install_auth(monkeypatch)
    resp = _client().get("/auth/frontchannel-logout", params={"sid": "nope"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache, no-store"


def test_frontchannel_logout_missing_sid_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sid at all still returns 200 + no-store (no-op)."""
    _install_auth(monkeypatch)
    resp = _client().get("/auth/frontchannel-logout")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache, no-store"
