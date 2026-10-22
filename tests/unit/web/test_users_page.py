"""Admin user-management HTML page tests (Wave 3, todo 18).

Exercises the ``/users`` admin page and its mutation routes rendered by
``users.html`` / :mod:`ovid2.web.routes.html`. The cookie / session setup
mirrors ``test_rest_gating.py`` and ``test_nav_visibility.py``;
``settings_svc.get_settings`` is monkeypatched to toggle ``auth.enabled``.

Covered acceptance criteria:

* admin GET ``/users`` renders the table plus a pending-approvals section
  exposing an Activate control and the pending user's email.
* every mutation form carries the CSRF hidden input.
* guest / operator GET ``/users`` → 403 (route-gated, not merely nav-hidden).
* unauthenticated GET ``/users`` → 401.
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
from ovid2.web.auth import csrf as csrf_mod
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
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key == b"set-cookie"
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


def _seed_pending(subject: str) -> str:
    """Create a pending user (default state); return its user_id."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    return str(record["user_id"])


def _set_cookie(client: TestClient, cookie: str | None) -> None:
    """Set (or clear) the ``ovid2_session`` cookie on the client instance."""
    client.cookies.clear()
    if cookie is not None:
        client.cookies.set("ovid2_session", cookie)


def _csrf_headers() -> dict[str, str]:
    """Return a header dict carrying a valid stateless CSRF token."""
    return {csrf_mod.CSRF_HEADER_NAME: csrf_mod.issue_token()}


# ---------------------------------------------------------------------------
# admin — full page renders with pending highlight + CSRF-guarded forms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_admin_users_page_renders_table_and_pending(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Admin GET /users shows the table, the pending row, and CSRF inputs."""
    _enable_auth(monkeypatch, enabled=True)
    admin_cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-users")
    _seed_pending("pending-approve")
    _set_cookie(app_client, admin_cookie)

    response = app_client.get("/users")
    assert response.status_code == 200
    body = response.text

    # The pending user's email appears with an Activate control.
    assert "pending-approve@example.org" in body
    assert 'action="/users/' in body
    assert "/activate" in body
    assert ">Activate<" in body
    # The admin's own row appears in the full table.
    assert "admin-users@example.org" in body
    # Every mutation form carries the CSRF hidden input.
    assert 'name="csrf_token"' in body
    # Set-role + disable/enable controls are present.
    assert "/role" in body
    assert "/disable" in body


@pytest.mark.unit
def test_admin_users_page_hides_internals(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """The page never leaks issuer / subject internals."""
    _enable_auth(monkeypatch, enabled=True)
    admin_cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-noleak")
    _set_cookie(app_client, admin_cookie)

    body = app_client.get("/users").text
    assert "accounts.google.com" not in body


@pytest.mark.unit
def test_users_error_banner_rendered(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An ``error`` query param surfaces as a flash banner."""
    _enable_auth(monkeypatch, enabled=True)
    admin_cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-error")
    _set_cookie(app_client, admin_cookie)

    body = app_client.get("/users?error=boom").text
    assert "flash error" in body
    assert "boom" in body


# ---------------------------------------------------------------------------
# non-admins are route-gated (not merely nav-hidden)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guest_users_page_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A guest lacks MANAGE_USERS → 403 on GET /users."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.GUEST, "guest-users"))
    assert app_client.get("/users").status_code == 403


@pytest.mark.unit
def test_operator_users_page_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A pipeline operator lacks MANAGE_USERS → 403 on GET /users."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.PIPELINE_OPERATOR, "op-users"))
    assert app_client.get("/users").status_code == 403


@pytest.mark.unit
def test_unauth_users_page_not_reachable(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """No session → the page is not served (401 or a login redirect)."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, None)
    status = app_client.get("/users").status_code
    assert status in (301, 302, 303, 307, 308, 401)


# ---------------------------------------------------------------------------
# mutation routes redirect and enforce the last-admin guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_admin_activate_redirects_and_activates(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """POST activate on a pending user redirects 303 and flips status."""
    _enable_auth(monkeypatch, enabled=True)
    admin_cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-act")
    pending_id = _seed_pending("pending-act")
    _set_cookie(app_client, admin_cookie)

    response = app_client.post(
        f"/users/{pending_id}/activate",
        data={"role": "viewer"},
        headers=_csrf_headers(),
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/users"
    record = users_svc.get_user(pending_id)
    assert record is not None
    assert record["status"] == "active"
    assert record["role"] == "viewer"


@pytest.mark.unit
def test_admin_invalid_role_redirects_with_error(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An invalid / pending role redirects with an error, never 500."""
    _enable_auth(monkeypatch, enabled=True)
    admin_cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-badrole")
    pending_id = _seed_pending("pending-badrole")
    _set_cookie(app_client, admin_cookie)

    response = app_client.post(
        f"/users/{pending_id}/activate",
        data={"role": "pending"},
        headers=_csrf_headers(),
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/users?error=")
    record = users_svc.get_user(pending_id)
    assert record is not None
    assert record["status"] == "pending"


@pytest.mark.unit
def test_last_admin_disable_guard_surfaces_error(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Disabling the last active admin redirects to /users?error=."""
    _enable_auth(monkeypatch, enabled=True)
    admin_record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="only-admin",
        email="only-admin@example.org",
        name="only-admin",
    )
    admin_record = users_svc.activate(
        user_id=admin_record["user_id"], role=Role.SYSTEM_ADMIN, actor="test"
    )
    session = sessions_svc.create(
        user_id=str(admin_record["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    _set_cookie(app_client, _signed_cookie_value(session.session_id))

    response = app_client.post(
        f"/users/{admin_record['user_id']}/disable", headers=_csrf_headers()
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/users?error=")
    record = users_svc.get_user(str(admin_record["user_id"]))
    assert record is not None
    assert record["status"] == "active"


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
