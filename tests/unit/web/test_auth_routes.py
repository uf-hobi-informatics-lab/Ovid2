"""Integration-lite tests for the OIDC + break-glass auth routes (todo 11).

Exercises :mod:`ovid2.web.routes.auth` end-to-end through a FastAPI
``TestClient`` built from :func:`ovid2.web.app.create_app`, with
authentication enabled and the OIDC provider **stubbed** — no network is
ever touched. The Authlib client's ``authorize_redirect`` /
``authorize_access_token`` are replaced by monkeypatching
:func:`ovid2.web.auth.oidc.get_registry` to hand back a fake registry.

Covered acceptance criteria:

* ``/login/{provider}`` 302s to an authorize URL carrying ``code_challenge``
  + ``state``.
* callback with a valid stubbed token → creates a session + sets the cookie
  + redirects; an open-redirect ``next=//evil.com`` is rejected (→ ``/``).
* callback with a mismatched ``state`` / ``nonce`` → 400, no session.
* ``/logout`` clears the cookie + revokes the session.
* break-glass POST with a correct / incorrect password.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.web import db as web_db
from ovid2.web.auth import breakglass, oidc
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.models import (
    AuthProviderConfig,
    GlobalSettings,
    SettingsAuth,
)
from ovid2.web.routes import auth as auth_routes
from ovid2.web.services import sessions_svc, settings_svc

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean web DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


def _auth_settings(*, enabled: bool = True) -> GlobalSettings:
    """Return a GlobalSettings with one enabled OIDC provider."""
    return GlobalSettings(
        auth=SettingsAuth(
            enabled=enabled,
            providers={
                "google": AuthProviderConfig(
                    display_name="Google",
                    issuer="https://accounts.google.com",
                    client_id="client-abc",
                )
            },
        )
    )


class _FakeClient:
    """A stubbed Authlib provider client.

    ``authorize_redirect`` writes ``state`` into the request's Authlib
    session (mirroring the real client) and returns a 302 to a fabricated
    authorize URL carrying ``state`` + ``code_challenge``.
    ``authorize_access_token`` returns a canned token whose ``userinfo``
    claims identify the user — unless ``token`` is set to raise.
    """

    def __init__(self, token: dict[str, Any] | None = None, *, raise_exchange: bool = False):
        self._token = token if token is not None else _DEFAULT_TOKEN
        self._raise_exchange = raise_exchange

    async def authorize_redirect(self, request: Any, redirect_uri: str, **kwargs: Any) -> Any:
        from starlette.responses import RedirectResponse

        # Mimic Authlib stashing state into the request session.
        session = request.scope.setdefault("session", {})
        session["_state_google_teststate"] = {"redirect_uri": redirect_uri}
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            "?response_type=code&client_id=client-abc"
            "&state=teststate&code_challenge=testchallenge"
            "&code_challenge_method=S256"
        )
        return RedirectResponse(url, status_code=302)

    async def authorize_access_token(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        if self._raise_exchange:
            raise ValueError("state mismatch")
        return self._token

    async def load_server_metadata(self) -> dict[str, Any]:
        return {"end_session_endpoint": "https://accounts.google.com/logout"}


class _FakeRegistry:
    """A stubbed Authlib ``OAuth`` registry that hands back a fixed client."""

    def __init__(self, client: _FakeClient):
        self._client = client

    def create_client(self, name: str) -> _FakeClient:
        return self._client


_DEFAULT_TOKEN: dict[str, Any] = {
    "access_token": "REDACTED-access",
    "id_token": "REDACTED-id",
    "userinfo": {
        "iss": "https://accounts.google.com",
        "sub": "subject-123",
        "email": "operator@example.org",
        "name": "Operator One",
        "sid": "idp-sid-xyz",
    },
}


def _install_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: _FakeClient | None = None,
    enabled: bool = True,
) -> None:
    """Enable auth and stub the OIDC registry across every call site."""
    settings = _auth_settings(enabled=enabled)
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    fake = _FakeRegistry(client or _FakeClient())
    monkeypatch.setattr(oidc, "get_registry", lambda _auth: fake)
    monkeypatch.setattr(
        auth_routes.oidc,
        "list_enabled_providers",
        lambda _auth: [oidc.EnabledProvider(key="google", display_name="Google")],
    )


def _client() -> Any:
    """Build a TestClient over a freshly-created app (no auto-redirect)."""
    from fastapi.testclient import TestClient

    from ovid2.web.app import create_app

    return TestClient(create_app(), follow_redirects=False)


# ---------------------------------------------------------------------------
# /login page
# ---------------------------------------------------------------------------


def test_login_page_renders_provider_button(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /login renders a button for each enabled provider."""
    _install_auth(monkeypatch)
    resp = _client().get("/login")
    assert resp.status_code == 200
    assert "Continue with Google" in resp.text


def test_login_page_shows_breakglass_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /login shows the break-glass form when a password is set."""
    _install_auth(monkeypatch)
    breakglass.set_password("hunter2-correct-horse", actor="test")
    resp = _client().get("/login")
    assert resp.status_code == 200
    assert "Break-glass" in resp.text


def test_login_page_redirects_when_already_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-authenticated (break-glass) session is redirected off /login."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="breakglass", idp_sid=None, absolute_ttl_min=60, idle_ttl_min=60
    )
    client = _client()
    client.cookies.set("ovid2_session", _signed(session.session_id))
    resp = client.get("/login?next=/runs")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/runs"


# ---------------------------------------------------------------------------
# /login/{provider}
# ---------------------------------------------------------------------------


def test_login_start_redirects_to_authorize_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /login/google 302s to an authorize URL with code_challenge+state."""
    _install_auth(monkeypatch)
    resp = _client().get("/login/google?next=/jobs")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "code_challenge=" in location
    assert "state=" in location
    # The transient authorize-flow cookie is set.
    set_cookie = "; ".join(resp.headers.get_list("set-cookie"))
    assert "ovid2_oidc_txn=" in set_cookie


def test_login_start_unknown_provider_redirects_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown provider bounces back to /login without a crash."""
    _install_auth(monkeypatch)
    resp = _client().get("/login/nope")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# /auth/callback/{provider}
# ---------------------------------------------------------------------------


def test_callback_creates_session_and_sets_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid callback mints a session, sets the cookie, and redirects to next."""
    _install_auth(monkeypatch)
    client = _client()
    # First hit /login/google to obtain the transient cookie.
    start = client.get("/login/google?next=/jobs")
    assert start.status_code == 302

    cb = client.get("/auth/callback/google")
    assert cb.status_code == 302
    assert cb.headers["location"] == "/jobs"
    set_cookie = "; ".join(cb.headers.get_list("set-cookie"))
    assert "ovid2_session=" in set_cookie
    # A session row now exists for the upserted user.
    # (We cannot read the opaque id here, but the DB has exactly one row.)
    with web_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
    assert count == 1


def test_callback_rejects_open_redirect_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol-relative next=//evil.com is rejected and falls back to /."""
    _install_auth(monkeypatch)
    client = _client()
    client.get("/login/google?next=//evil.com")
    cb = client.get("/auth/callback/google")
    assert cb.status_code == 302
    assert cb.headers["location"] == "/"


def test_callback_without_transient_cookie_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A callback with no transient cookie (missing state) → 400, no session."""
    _install_auth(monkeypatch)
    resp = _client().get("/auth/callback/google")
    assert resp.status_code == 400
    with web_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
    assert count == 0


def test_callback_state_mismatch_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token exchange that raises (bad state/nonce) → 400, no session."""
    _install_auth(monkeypatch, client=_FakeClient(raise_exchange=True))
    client = _client()
    client.get("/login/google?next=/")
    cb = client.get("/auth/callback/google")
    assert cb.status_code == 400
    with web_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
    assert count == 0


def test_callback_missing_subject_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token with no subject claim is rejected with 400."""
    token = {"userinfo": {"iss": "https://accounts.google.com", "email": "x@example.org"}}
    _install_auth(monkeypatch, client=_FakeClient(token=token))
    client = _client()
    client.get("/login/google?next=/")
    cb = client.get("/auth/callback/google")
    assert cb.status_code == 400


# ---------------------------------------------------------------------------
# break-glass POST
# ---------------------------------------------------------------------------


def test_breakglass_correct_password_creates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correct break-glass password mints a session and sets the cookie."""
    _install_auth(monkeypatch)
    breakglass.set_password("correct-horse-battery", actor="test")
    resp = _client().post(
        "/login/breakglass",
        data={"password": "correct-horse-battery", "next": "/runs"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/runs"
    set_cookie = "; ".join(resp.headers.get_list("set-cookie"))
    assert "ovid2_session=" in set_cookie


def test_breakglass_wrong_password_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """An incorrect break-glass password re-renders /login with an error."""
    _install_auth(monkeypatch)
    breakglass.set_password("correct-horse-battery", actor="test")
    resp = _client().post(
        "/login/breakglass",
        data={"password": "totally-wrong", "next": "/"},
    )
    assert resp.status_code == 401
    assert "Incorrect break-glass password." in resp.text
    with web_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------


def test_logout_revokes_session_and_clears_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /logout revokes the session and clears the cookie."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="breakglass", idp_sid=None, absolute_ttl_min=60, idle_ttl_min=60
    )
    assert sessions_svc.get_valid(session.session_id) is not None

    client = _client()
    client.cookies.set("ovid2_session", _signed(session.session_id))
    resp = client.get("/logout")
    assert resp.status_code == 302
    # No IdP end-session because idp_sid is None → falls back to /login.
    assert resp.headers["location"] == "/login"
    set_cookie = "; ".join(resp.headers.get_list("set-cookie"))
    assert "ovid2_session=" in set_cookie  # deletion Set-Cookie present
    # The session is now revoked.
    assert sessions_svc.get_valid(session.session_id) is None


def test_logout_redirects_to_end_session_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with an idp_sid logs out through the IdP end-session URL."""
    _install_auth(monkeypatch)
    session = sessions_svc.create(
        user_id="user-1", idp_sid="idp-sid-xyz", absolute_ttl_min=60, idle_ttl_min=60
    )
    client = _client()
    client.cookies.set("ovid2_session", _signed(session.session_id))
    resp = client.get("/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://accounts.google.com/logout"
    assert sessions_svc.get_valid(session.session_id) is None


# ---------------------------------------------------------------------------
# _safe_next unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("   ", "/"),
        ("/runs", "/runs"),
        ("/runs?x=1", "/runs?x=1"),
        ("//evil.com", "/"),
        ("http://evil.com", "/"),
        ("https://evil.com/x", "/"),
        ("\\\\evil.com", "/"),
        ("/\\evil.com", "/"),
        ("javascript:alert(1)", "/"),
        ("mailto:x@y.com", "/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    """_safe_next accepts only local paths and defaults everything else to /."""
    assert auth_routes._safe_next(value) == expected


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _signed(session_id: str) -> str:
    """Return the signed cookie value ``cookie.issue_cookie`` would emit."""
    from http.cookies import SimpleCookie

    from starlette.responses import Response

    response = Response()
    cookie_mod.issue_cookie(response, session_id)
    line = next(
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key == b"set-cookie"
    )
    jar: SimpleCookie[str] = SimpleCookie()
    jar.load(line)
    return jar["ovid2_session"].value
