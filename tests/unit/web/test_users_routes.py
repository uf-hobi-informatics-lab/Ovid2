"""User-management REST endpoint tests (Wave 3, todo 17).

Exercises the ``/api/v1/users`` routes wired to ``users_svc`` and gated by
``Capability.MANAGE_USERS``.

Strategy
--------
Mirrors :mod:`test_rest_gating`: auth is forced on via a
``settings_svc.get_settings`` patch, a real session + user row is created
through ``sessions_svc`` / ``users_svc``, and the signed ``ovid2_session``
cookie is threaded into the ``TestClient``. An admin context drives the
happy paths (list / activate / role / disable), the last-admin guard is
asserted to answer 409, and a viewer context is asserted to answer 403.
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


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` over a freshly constructed app."""
    client = TestClient(create_app())
    try:
        yield client
    finally:
        client.close()


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``settings_svc.get_settings`` to report auth enabled."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=True))
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


def _make_active_user(role: Role, subject: str) -> str:
    """Create an active user in ``role``; return its user_id."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    record = users_svc.activate(user_id=record["user_id"], role=role, actor="test")
    return str(record["user_id"])


def _make_pending_user(subject: str) -> str:
    """Create a pending user (default state); return its user_id."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    return str(record["user_id"])


def _cookie_for_user(user_id: str) -> str:
    """Create a session for ``user_id`` and return its signed cookie."""
    session = sessions_svc.create(
        user_id=user_id,
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    return _signed_cookie_value(session.session_id)


def _set_cookie(client: TestClient, cookie: str) -> None:
    """Set the ``ovid2_session`` cookie on the client instance."""
    client.cookies.clear()
    client.cookies.set("ovid2_session", cookie)


@pytest.mark.unit
def test_admin_lists_users(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """An admin can list users, and a status filter narrows the set."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-list")
    _make_pending_user("pending-list")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.get("/api/v1/users")
    assert resp.status_code == 200
    ids = {row["user_id"] for row in resp.json()}
    assert admin_id in ids

    resp_active = app_client.get("/api/v1/users", params={"status": "active"})
    assert resp_active.status_code == 200
    active_ids = {row["user_id"] for row in resp_active.json()}
    assert admin_id in active_ids
    assert all(row["status"] == "active" for row in resp_active.json())


@pytest.mark.unit
def test_admin_gets_user_and_404_on_unknown(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """GET one user returns the full record; unknown id → 404."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-get")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.get(f"/api/v1/users/{admin_id}")
    assert resp.status_code == 200
    assert resp.json()["role"] == Role.SYSTEM_ADMIN.value

    assert app_client.get("/api/v1/users/does-not-exist").status_code == 404


@pytest.mark.unit
def test_admin_activates_pending_user(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An admin activates a pending user with a concrete role."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-activate")
    target_id = _make_pending_user("target-activate")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.post(f"/api/v1/users/{target_id}/activate", json={"role": "viewer"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == Role.VIEWER.value
    assert body["status"] == "active"


@pytest.mark.unit
def test_admin_activate_rejects_pending_role(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Assigning role='pending' is rejected with 400."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-activate-bad")
    target_id = _make_pending_user("target-activate-bad")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.post(f"/api/v1/users/{target_id}/activate", json={"role": "pending"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_admin_changes_role(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """An admin changes an active user's role."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-role")
    target_id = _make_active_user(Role.VIEWER, "target-role")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.put(f"/api/v1/users/{target_id}/role", json={"role": "pipeline_operator"})
    assert resp.status_code == 200
    assert resp.json()["role"] == Role.PIPELINE_OPERATOR.value


@pytest.mark.unit
def test_admin_disables_user(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """An admin disables a non-admin user."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-disable")
    target_id = _make_active_user(Role.VIEWER, "target-disable")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.post(f"/api/v1/users/{target_id}/disable")
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


@pytest.mark.unit
def test_disable_last_admin_conflicts(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Disabling the sole active admin → 409 (last-admin guard)."""
    _enable_auth(monkeypatch)
    admin_id = _make_active_user(Role.SYSTEM_ADMIN, "admin-only")
    _set_cookie(app_client, _cookie_for_user(admin_id))

    resp = app_client.post(f"/api/v1/users/{admin_id}/disable")
    assert resp.status_code == 409


@pytest.mark.unit
def test_viewer_forbidden(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """A viewer lacks MANAGE_USERS → 403 on the users list."""
    _enable_auth(monkeypatch)
    viewer_id = _make_active_user(Role.VIEWER, "viewer-users")
    _set_cookie(app_client, _cookie_for_user(viewer_id))

    assert app_client.get("/api/v1/users").status_code == 403


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
