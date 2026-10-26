"""Integration: OIDC login round-trip against a stubbed provider (todo 23).

This is the end-to-end proof that the auth stack actually works when
``settings.auth.enabled`` is ``True`` — exercised through the *real*
:func:`ovid2.web.app.create_app` app and the *real* Authlib token-validation
path, with a **hermetic fake OIDC provider** standing in for Google/Microsoft.
No live network is ever touched (marker: ``integration``; no ``real_connection``
marker, so this runs in the default integration selection).

What makes this a genuine round-trip (vs. the ``test_auth_routes.py`` unit
suite, which stubs the Authlib client and returns a canned token dict):

* A real RS256 keypair is generated per test.
* A fake IdP is mounted as a Starlette ASGI app exposing the three OIDC
  endpoints Authlib fetches: ``/.well-known/openid-configuration`` (discovery),
  ``/jwks`` (the public key set), and ``/token`` (the code→token exchange).
* Authlib is pointed at that app via an :class:`httpx2.ASGITransport`, so the
  discovery fetch, the JWKS fetch, the code exchange, **and the RS256 ID-token
  signature + nonce validation all run for real** inside
  :meth:`authorize_access_token`.
* The provider's authorize redirect is intercepted; the ``state`` + ``nonce``
  Authlib minted are lifted out and folded into the ``code`` the fake IdP
  later exchanges — so the ID token carries the exact nonce Authlib expects.

Coverage (the plan's acceptance criteria):

* login → callback → session cookie set, user created ``pending``;
* bootstrap-admin allowlist path activates the same user to ``system_admin``;
* an authenticated request to a gated endpoint then succeeds;
* ``/logout`` revokes the session (next request bounces to ``/login``);
* idle-expiry (clock advanced past the idle TTL) forces re-login.
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from authlib.integrations.starlette_client import OAuth
from authlib.jose import JsonWebKey, jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

pytestmark = pytest.mark.integration

_ISSUER = "https://idp.test"
_CLIENT_ID = "client-abc"
_CLIENT_SECRET = "client-secret-xyz"
_PROVIDER = "google"
_BOOTSTRAP_EMAIL = "admin@example.org"


# ---------------------------------------------------------------------------
# XDG isolation (self-contained: this test lives outside tests/unit/web)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every XDG path into ``tmp_path`` and reload settings."""
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path / "runtime" / "ovid2"))
    monkeypatch.setenv("OVID2_SCHEDULER_DB", str(tmp_path / "state" / "ovid2" / "scheduler.db"))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(tmp_path / "config" / "ovid2" / "configs"))
    monkeypatch.setenv("OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json"))
    (tmp_path / "config" / "ovid2" / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ovid2").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "ovid2").mkdir(parents=True, exist_ok=True)

    import ovid2.config.settings as _settings

    importlib.reload(_settings)
    yield tmp_path


# ---------------------------------------------------------------------------
# Fake OIDC provider (a real ASGI app Authlib fetches over ASGITransport)
# ---------------------------------------------------------------------------


class _FakeIdp:
    """A minimal but real OIDC provider mounted as a Starlette ASGI app.

    Serves discovery, JWKS, and the token endpoint. The token endpoint
    decodes the ``code`` (an opaque JSON blob the test embeds the pending
    ``nonce`` + user claims into) and returns a genuinely RS256-signed ID
    token so Authlib's signature + nonce validation runs for real.
    """

    def __init__(self) -> None:
        self._key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
        self._kid = self._key.thumbprint()
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery),
                Route("/jwks", self._jwks),
                Route("/token", self._token, methods=["POST"]),
            ]
        )

    async def _discovery(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": _ISSUER,
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "jwks_uri": f"{_ISSUER}/jwks",
                "end_session_endpoint": f"{_ISSUER}/logout",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    def _public_jwk(self) -> dict[str, Any]:
        pub: dict[str, Any] = dict(self._key.as_dict(is_private=False))
        pub.update({"kid": self._kid, "use": "sig", "alg": "RS256"})
        return pub

    async def _jwks(self, request: Request) -> JSONResponse:
        return JSONResponse({"keys": [self._public_jwk()]})

    async def _token(self, request: Request) -> JSONResponse:
        form = await request.form()
        code = form.get("code", "")
        try:
            payload = json.loads(bytes.fromhex(str(code)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        claims = {
            "iss": _ISSUER,
            "sub": payload["sub"],
            "aud": _CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": payload["nonce"],
        }
        for optional in ("email", "name", "sid"):
            if optional in payload:
                claims[optional] = payload[optional]
        header = {"alg": "RS256", "kid": self._kid}
        id_token = jwt.encode(header, claims, self._key).decode("ascii")
        return JSONResponse(
            {
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            }
        )


def _encode_code(*, sub: str, nonce: str, **claims: str) -> str:
    """Encode the pending user claims + nonce into an opaque ``code`` string."""
    blob: dict[str, str] = {"sub": sub, "nonce": nonce, **claims}
    return json.dumps(blob).encode("utf-8").hex()


# ---------------------------------------------------------------------------
# Test harness: wire the real app + real Authlib at the fake IdP
# ---------------------------------------------------------------------------


class _Harness:
    """Bundles the app TestClient, the fake IdP, and the login driver."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, bootstrap: bool) -> None:
        from fastapi.testclient import TestClient

        from ovid2.web import db as web_db
        from ovid2.web.app import create_app
        from ovid2.web.auth import cookie as cookie_mod
        from ovid2.web.auth import oidc
        from ovid2.web.models import AuthProviderConfig, GlobalSettings, SettingsAuth
        from ovid2.web.services import settings_svc

        self._web_db = web_db
        self._cookie_mod = cookie_mod
        web_db.ensure_schema()
        cookie_mod.reset_signing_key_cache()

        self.idp = _FakeIdp()
        # httpx transport that routes every Authlib call into the fake IdP app.
        transport = httpx2.ASGITransport(app=self.idp.app)

        settings = GlobalSettings(
            auth=SettingsAuth(
                enabled=True,
                providers={
                    _PROVIDER: AuthProviderConfig(
                        display_name="Google",
                        issuer=_ISSUER,
                        client_id=_CLIENT_ID,
                    )
                },
                bootstrap_admin_emails=[_BOOTSTRAP_EMAIL] if bootstrap else [],
                idle_ttl_minutes=45,
                absolute_ttl_minutes=480,
            )
        )
        monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)

        def _real_registry(_auth: Any) -> OAuth:
            oauth = OAuth()
            oauth.register(
                name=_PROVIDER,
                client_id=_CLIENT_ID,
                client_secret=_CLIENT_SECRET,
                server_metadata_url=f"{_ISSUER}/.well-known/openid-configuration",
                client_kwargs={
                    "scope": "openid email profile",
                    "code_challenge_method": "S256",
                    "transport": transport,
                },
            )
            return oauth

        monkeypatch.setattr(oidc, "get_registry", _real_registry)
        monkeypatch.setattr(
            oidc,
            "list_enabled_providers",
            lambda _auth: [oidc.EnabledProvider(key=_PROVIDER, display_name="Google")],
        )

        self.client = TestClient(create_app(), follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def login(self, *, sub: str, email: str, name: str, sid: str = "idp-sid-1") -> Any:
        """Drive /login/{provider} → intercept redirect → hit the callback.

        Returns the callback response (302 on success, carrying the session
        cookie), having threaded the exact ``state`` + ``nonce`` Authlib minted
        through the fake IdP's token endpoint.
        """
        start = self.client.get(f"/login/{_PROVIDER}?next=/runs")
        assert start.status_code == 302, start.text
        location = start.headers["location"]
        query = parse_qs(urlsplit(location).query)
        state = query["state"][0]
        nonce = query["nonce"][0]
        code = _encode_code(sub=sub, nonce=nonce, email=email, name=name, sid=sid)
        return self.client.get(f"/auth/callback/{_PROVIDER}?code={code}&state={state}")

    def session_count(self) -> int:
        with self._web_db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0])

    def user_row(self, email: str) -> dict[str, Any] | None:
        with self._web_db.connect() as conn:
            row = conn.execute(
                "SELECT user_id, role, status FROM web_users WHERE lower(email)=lower(?)",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "role": row[1], "status": row[2]}


@pytest.fixture()
def make_harness(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Factory yielding a configured harness; tears down the client."""
    built: list[_Harness] = []

    def _factory(*, bootstrap: bool = False) -> _Harness:
        h = _Harness(monkeypatch, bootstrap=bootstrap)
        built.append(h)
        return h

    try:
        yield _factory
    finally:
        for h in built:
            h.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_login_creates_pending_user_and_session(make_harness: Any) -> None:
    """A first login through the real Authlib path mints a pending user + session."""
    h = make_harness(bootstrap=False)
    cb = h.login(sub="sub-1", email="newbie@example.org", name="New Bie")
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/runs"
    set_cookie = "; ".join(cb.headers.get_list("set-cookie"))
    assert "ovid2_session=" in set_cookie

    user = h.user_row("newbie@example.org")
    assert user is not None
    assert user["status"] == "pending"
    assert user["role"] == "pending"
    assert h.session_count() == 1


@pytest.mark.integration
def test_bootstrap_admin_activates_and_can_reach_gated_endpoint(make_harness: Any) -> None:
    """An allowlisted first login becomes system_admin and passes a gated route."""
    h = make_harness(bootstrap=True)
    cb = h.login(sub="sub-admin", email=_BOOTSTRAP_EMAIL, name="Admin User")
    assert cb.status_code == 302, cb.text

    user = h.user_row(_BOOTSTRAP_EMAIL)
    assert user is not None
    assert user["status"] == "active"
    assert user["role"] == "system_admin"

    # The session cookie carried by the client now authorizes a gated endpoint.
    gated = h.client.get("/api/v1/users")
    assert gated.status_code == 200, gated.text


@pytest.mark.integration
def test_logout_revokes_session_and_next_request_redirects(make_harness: Any) -> None:
    """After /logout the session is dead and a gated HTML page bounces to /login."""
    h = make_harness(bootstrap=True)
    cb = h.login(sub="sub-admin", email=_BOOTSTRAP_EMAIL, name="Admin User")
    assert cb.status_code == 302

    # Authenticated HTML page works before logout.
    assert h.client.get("/runs").status_code == 200

    logout = h.client.get("/logout")
    assert logout.status_code == 302

    after = h.client.get("/runs")
    assert after.status_code == 302
    assert after.headers["location"].startswith("/login")


@pytest.mark.integration
def test_idle_expiry_forces_relogin(make_harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advancing the clock past the idle TTL invalidates the session."""
    h = make_harness(bootstrap=True)
    cb = h.login(sub="sub-admin", email=_BOOTSTRAP_EMAIL, name="Admin User")
    assert cb.status_code == 302
    assert h.client.get("/runs").status_code == 200

    # Advance time well past the 45-minute idle TTL. sessions_svc reads the
    # wall clock via datetime.now(UTC); monkeypatch its clock forward.
    from datetime import datetime, timedelta, timezone

    from ovid2.web.services import sessions_svc

    real_now = datetime.now(timezone.utc)
    future = real_now + timedelta(minutes=46)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return future if tz is None else future.astimezone(tz)

    monkeypatch.setattr(sessions_svc, "datetime", _FrozenDatetime)

    after = h.client.get("/runs")
    assert after.status_code == 302
    assert after.headers["location"].startswith("/login")


if __name__ == "__main__":
    raise SystemExit("integration test module; run via pytest.")
