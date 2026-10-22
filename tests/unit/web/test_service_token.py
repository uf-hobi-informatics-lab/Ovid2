"""Unit tests for the service-principal token verifier (todo 21).

Covers:

- ``verify_token`` resolves a known token to its mapped role and returns
  ``None`` for an unknown / empty token.
- A token whose stored role is missing/invalid falls back to
  ``settings.auth.service_principal_role`` (never ``system_admin``).
- ``current_user`` accepts a valid Bearer token when auth is enabled and
  no session cookie is present, yielding an operator ``AuthContext``.
- A gated endpoint returns 401 without a valid token and 403 when the
  operator token hits a MANAGE_USERS-tier endpoint (operator lacks it).
- A session cookie still takes precedence over the Bearer token.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import deps, service_token
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import settings_svc


@pytest.fixture(autouse=True)
def _temp_db(isolated_xdg: Path) -> None:
    """Provision a clean auth DB inside the isolated XDG tree."""
    web_db.ensure_schema()


def _write_token_map(mapping: dict[str, str]) -> None:
    """Write ``{sha256hex: role}`` to the service-token secret file (0o600)."""
    secrets_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / f"{service_token.SECRET_NAME}.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    path.chmod(0o600)


def _hash(token: str) -> str:
    """Return the SHA-256 hex digest of ``token`` (matches the verifier)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _use_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    service_role: str = "pipeline_operator",
) -> None:
    """Force ``settings_svc.get_settings`` to a known auth configuration."""
    settings = GlobalSettings(
        auth=SettingsAuth(enabled=enabled, service_principal_role=service_role)
    )
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _bearer_request(token: str | None) -> Request:
    """Build a minimal Starlette request carrying an optional Bearer header."""
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_configured_false_without_secret(isolated_xdg: Path) -> None:
    """No secret file → not configured, and any token verifies to None."""
    assert service_token.is_configured() is False
    assert service_token.verify_token("anything") is None


@pytest.mark.unit
def test_verify_token_resolves_known_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known token digest resolves to its mapped role."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    assert service_token.is_configured() is True
    assert service_token.verify_token("op-token") == Role.PIPELINE_OPERATOR


@pytest.mark.unit
def test_verify_token_unknown_and_empty_return_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown and empty tokens resolve to None."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "viewer"})
    assert service_token.verify_token("nope") is None
    assert service_token.verify_token("") is None


@pytest.mark.unit
def test_verify_token_invalid_role_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored role that isn't a known Role collapses to the fallback."""
    _use_settings(monkeypatch, service_role="viewer")
    _write_token_map({_hash("weird"): "not_a_real_role"})
    assert service_token.verify_token("weird") == Role.VIEWER


@pytest.mark.unit
def test_verify_token_never_returns_system_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token mapped to system_admin is downgraded to the fallback."""
    _use_settings(monkeypatch, service_role="pipeline_operator")
    _write_token_map({_hash("admin-attempt"): "system_admin"})
    resolved = service_token.verify_token("admin-attempt")
    assert resolved != Role.SYSTEM_ADMIN
    assert resolved == Role.PIPELINE_OPERATOR


@pytest.mark.unit
def test_fallback_admin_setting_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a misconfigured admin fallback can't escalate a token."""
    _use_settings(monkeypatch, service_role="system_admin")
    _write_token_map({_hash("bad"): "also_bogus"})
    resolved = service_token.verify_token("bad")
    assert resolved != Role.SYSTEM_ADMIN
    assert resolved == Role.PIPELINE_OPERATOR


# ---------------------------------------------------------------------------
# current_user integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_current_user_accepts_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid Bearer token with no cookie resolves to an operator context."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    ctx = deps.current_user(_bearer_request("op-token"))
    assert ctx is not None
    assert ctx.role == Role.PIPELINE_OPERATOR
    assert ctx.user_id == "mcp:service"
    assert ctx.email is None
    assert ctx.anonymous_bypass is False


@pytest.mark.unit
def test_current_user_rejects_absent_and_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token / an unknown token → None (→ 401 on a gated endpoint)."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    assert deps.current_user(_bearer_request(None)) is None
    assert deps.current_user(_bearer_request("bogus")) is None


@pytest.mark.unit
def test_current_user_ignores_non_bearer_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Basic (non-Bearer) Authorization header is ignored."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    request = _bearer_request(None)
    scope = dict(request.scope)
    scope["headers"] = [(b"authorization", b"Basic op-token")]
    assert deps.current_user(Request(scope)) is None


# ---------------------------------------------------------------------------
# REST gating end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` over a freshly constructed app."""
    client = TestClient(create_app())
    try:
        yield client
    finally:
        client.close()


@pytest.mark.unit
def test_gated_endpoint_401_without_token(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Auth enabled, no token → 401 on a gated endpoint."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    assert app_client.get("/api/v1/pipelines").status_code == 401


@pytest.mark.unit
def test_operator_token_reaches_submit_tier(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """The operator token passes the VIEW_PIPELINE_DATA gate."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    resp = app_client.get(
        "/api/v1/pipelines", headers={"Authorization": "Bearer op-token"}
    )
    assert resp.status_code == 200


@pytest.mark.unit
def test_operator_token_cannot_manage_users(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """The operator token cannot reach a MANAGE_USERS-tier endpoint → 403."""
    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    resp = app_client.get(
        "/api/v1/secrets", headers={"Authorization": "Bearer op-token"}
    )
    assert resp.status_code == 403


@pytest.mark.unit
def test_gated_dependency_401_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A require_capability dependency raises 401 for an absent token."""
    from ovid2.web.auth.roles import Capability

    _use_settings(monkeypatch)
    _write_token_map({_hash("op-token"): "pipeline_operator"})
    dep = deps.require_capability(Capability.VIEW_PIPELINE_DATA)
    with pytest.raises(HTTPException) as exc:
        dep(_bearer_request(None))
    assert exc.value.status_code == 401


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
