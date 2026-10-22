"""Unit tests for the auth dependency layer (Wave 2, todo 9).

Covers the plan's acceptance criteria for :mod:`ovid2.web.auth.deps`:

- ``enabled=False`` → ``current_user`` returns the synthetic admin bypass and a
  ``require_capability`` dependency passes.
- ``enabled=True`` + no cookie → the gated dependency raises ``401``.
- ``enabled=True`` + active viewer hitting an admin-only cap → ``403``.
- ``enabled=True`` + active operator hitting ``SUBMIT_JOBS`` → allowed.
- ``enabled=True`` + pending user (valid session, role=pending) → ``403``.

Real sessions / users are built through ``sessions_svc.create`` +
``users_svc.upsert_from_idp`` / ``activate`` against the isolated test DB. The
signed cookie is produced by ``cookie.issue_cookie`` and threaded into a
minimal Starlette ``Request`` (mirroring ``test_cookie.py``).
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ovid2.web import db as web_db
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth import deps
from ovid2.web.auth.roles import Capability, Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import sessions_svc, settings_svc, users_svc


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


def _use_auth(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force ``settings_svc.get_settings`` to a known ``auth.enabled`` value."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    # ``deps`` and ``cookie`` both read settings through ``settings_svc`` by
    # module attribute, so patching there covers every call site.


def _make_request(cookies: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette ``Request`` carrying the given cookies."""
    header_list: list[tuple[bytes, bytes]] = []
    if cookies:
        jar: SimpleCookie[str] = SimpleCookie()
        for name, value in cookies.items():
            jar[name] = value
        cookie_header = "; ".join(f"{k}={m.value}" for k, m in jar.items())
        header_list.append((b"cookie", cookie_header.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": header_list,
        "server": ("testserver", 80),
    }
    return Request(scope)


def _signed_cookie_value(session_id: str) -> str:
    """Return the signed cookie value ``cookie.issue_cookie`` would emit."""
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


def _request_for_session(session_id: str) -> Request:
    """Build a request carrying a valid signed cookie for ``session_id``."""
    return _make_request(cookies={"ovid2_session": _signed_cookie_value(session_id)})


def _make_active_user(subject: str, role: Role) -> dict[str, object]:
    """Create an IdP user and activate it into ``role``. Returns the record."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    return users_svc.activate(user_id=record["user_id"], role=role, actor="test")


def _make_pending_user(subject: str) -> dict[str, object]:
    """Create an IdP user and leave it in the default pending state."""
    return users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )


# ---------------------------------------------------------------------------
# enabled=False → synthetic admin bypass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_auth_returns_synthetic_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """current_user returns the SYSTEM_ADMIN bypass when auth is disabled."""
    _use_auth(monkeypatch, enabled=False)
    ctx = deps.current_user(_make_request())
    assert ctx is not None
    assert ctx.role == Role.SYSTEM_ADMIN
    assert ctx.user_id == deps.ANONYMOUS_USER_ID
    assert ctx.email is None
    assert ctx.anonymous_bypass is True


@pytest.mark.unit
def test_disabled_auth_capability_dependency_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_capability passes for the bypass admin when auth is disabled."""
    _use_auth(monkeypatch, enabled=False)
    dep = deps.require_capability(Capability.MANAGE_USERS)
    ctx = dep(_make_request())
    assert ctx.role == Role.SYSTEM_ADMIN
    assert ctx.anonymous_bypass is True


@pytest.mark.unit
def test_disabled_auth_role_dependency_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_role passes for the bypass admin when auth is disabled."""
    _use_auth(monkeypatch, enabled=False)
    dep = deps.require_role(Role.PIPELINE_OPERATOR)
    ctx = dep(_make_request())
    assert ctx.role == Role.SYSTEM_ADMIN


# ---------------------------------------------------------------------------
# enabled=True → real resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enabled_no_cookie_current_user_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cookie → current_user returns None when auth is enabled."""
    _use_auth(monkeypatch, enabled=True)
    assert deps.current_user(_make_request()) is None


@pytest.mark.unit
def test_enabled_no_cookie_capability_dependency_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cookie → require_capability raises 401 when auth is enabled."""
    _use_auth(monkeypatch, enabled=True)
    dep = deps.require_capability(Capability.VIEW_RUNS)
    with pytest.raises(HTTPException) as exc:
        dep(_make_request())
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_enabled_no_cookie_role_dependency_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cookie → require_role raises 401 when auth is enabled."""
    _use_auth(monkeypatch, enabled=True)
    dep = deps.require_role(Role.VIEWER)
    with pytest.raises(HTTPException) as exc:
        dep(_make_request())
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_enabled_active_viewer_admin_cap_raises_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active viewer hitting an admin-only cap → 403."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("viewer-1", Role.VIEWER)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_capability(Capability.MANAGE_USERS)
    with pytest.raises(HTTPException) as exc:
        dep(_request_for_session(session.session_id))
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_enabled_active_operator_submit_cap_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active operator hitting SUBMIT_JOBS → allowed, returns their context."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("operator-1", Role.PIPELINE_OPERATOR)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_capability(Capability.SUBMIT_JOBS)
    ctx = dep(_request_for_session(session.session_id))
    assert ctx.role == Role.PIPELINE_OPERATOR
    assert ctx.user_id == str(user["user_id"])
    assert ctx.email == "operator-1@example.org"
    assert ctx.anonymous_bypass is False


@pytest.mark.unit
def test_enabled_pending_user_current_user_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending user (valid session) resolves to a PENDING AuthContext."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_pending_user("pending-1")
    assert user["role"] == Role.PENDING.value
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    ctx = deps.current_user(_request_for_session(session.session_id))
    assert ctx is not None
    assert ctx.role == Role.PENDING


@pytest.mark.unit
def test_enabled_pending_user_capability_raises_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending user (authenticated) hitting any cap → 403, not 401."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_pending_user("pending-2")
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_capability(Capability.VIEW_PUBLIC)
    with pytest.raises(HTTPException) as exc:
        dep(_request_for_session(session.session_id))
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_enabled_session_for_deleted_user_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid session pointing at a non-existent user row → None (401 later)."""
    _use_auth(monkeypatch, enabled=True)
    session = sessions_svc.create(
        user_id="ghost-user-id",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    assert deps.current_user(_request_for_session(session.session_id)) is None


@pytest.mark.unit
def test_enabled_tampered_cookie_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cookie that fails signature verification → None."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("viewer-2", Role.VIEWER)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    signed = _signed_cookie_value(session.session_id)
    tampered = signed[:-1] + ("A" if signed[-1] != "A" else "B")
    request = _make_request(cookies={"ovid2_session": tampered})
    assert deps.current_user(request) is None


@pytest.mark.unit
def test_enabled_role_dependency_exact_match_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_role passes on an exact role match."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("viewer-3", Role.VIEWER)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_role(Role.VIEWER)
    ctx = dep(_request_for_session(session.session_id))
    assert ctx.role == Role.VIEWER


@pytest.mark.unit
def test_enabled_role_dependency_system_admin_always_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system_admin satisfies a require_role gate for a different role."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("admin-1", Role.SYSTEM_ADMIN)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_role(Role.PIPELINE_OPERATOR)
    ctx = dep(_request_for_session(session.session_id))
    assert ctx.role == Role.SYSTEM_ADMIN


@pytest.mark.unit
def test_enabled_role_dependency_wrong_role_raises_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer hitting require_role(PIPELINE_OPERATOR) → 403."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("viewer-4", Role.VIEWER)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    dep = deps.require_role(Role.PIPELINE_OPERATOR)
    with pytest.raises(HTTPException) as exc:
        dep(_request_for_session(session.session_id))
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_enabled_touch_slides_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful resolution slides the session idle window (touch called)."""
    _use_auth(monkeypatch, enabled=True)
    user = _make_active_user("operator-2", Role.PIPELINE_OPERATOR)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    touched: list[tuple[str, int]] = []
    real_touch = sessions_svc.touch

    def _spy_touch(session_id: str, idle_ttl_min: int):  # type: ignore[no-untyped-def]
        touched.append((session_id, idle_ttl_min))
        return real_touch(session_id, idle_ttl_min)

    monkeypatch.setattr(sessions_svc, "touch", _spy_touch)
    ctx = deps.current_user(_request_for_session(session.session_id))
    assert ctx is not None
    assert touched == [(session.session_id, 45)]
