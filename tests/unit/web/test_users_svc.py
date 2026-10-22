from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web.auth.roles import Role
from ovid2.web.services import users_svc


@pytest.mark.unit
def test_first_upsert_is_pending(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="sub-1",
        email="a@example.org",
        name="A",
    )
    assert record["role"] == Role.PENDING.value
    assert record["status"] == Role.PENDING.value


@pytest.mark.unit
def test_activate_sets_role_and_status(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="sub-1",
        email="a@example.org",
        name="A",
    )
    user_id = record["user_id"]
    activated = users_svc.activate(user_id=user_id, role=Role.VIEWER, actor="admin")
    assert activated["role"] == Role.VIEWER.value
    assert activated["status"] == "active"
    # Reload via get_user
    reloaded = users_svc.get_user(user_id)
    assert reloaded is not None
    assert reloaded["role"] == Role.VIEWER.value
    assert reloaded["status"] == "active"
    # Check list_users with status filter
    active_users = users_svc.list_users(status="active")
    active_ids = {u["user_id"] for u in active_users}
    assert user_id in active_ids


@pytest.mark.unit
def test_reupsert_preserves_role_status(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="sub-1",
        email="a@example.org",
        name="A",
    )
    user_id = record["user_id"]
    # Activate to VIEWER/active
    users_svc.activate(user_id=user_id, role=Role.VIEWER, actor="admin")
    # Re-upsert with new email
    users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="sub-1",
        email="a2@example.org",
        name="A New Name",
    )
    reloaded = users_svc.get_user(user_id)
    assert reloaded is not None
    assert reloaded["email"] == "a2@example.org"
    assert reloaded["name"] == "A New Name"
    assert reloaded["role"] == Role.VIEWER.value
    assert reloaded["status"] == "active"


@pytest.mark.unit
def test_last_admin_guard(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    # Create first admin
    record1 = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="admin-1",
        email="admin1@example.org",
        name="Admin One",
    )
    uid1 = record1["user_id"]
    users_svc.activate(user_id=uid1, role=Role.SYSTEM_ADMIN, actor="actor")
    # Attempt to disable last admin -> should raise
    with pytest.raises(users_svc.UserError):
        users_svc.disable(user_id=uid1, actor="actor")
    # Attempt to set_role away from system_admin -> should raise
    with pytest.raises(users_svc.UserError):
        users_svc.set_role(user_id=uid1, role=Role.VIEWER, actor="actor")
    # Create second admin
    record2 = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="admin-2",
        email="admin2@example.org",
        name="Admin Two",
    )
    uid2 = record2["user_id"]
    users_svc.activate(user_id=uid2, role=Role.SYSTEM_ADMIN, actor="actor")
    # Now disabling first admin should succeed
    disabled = users_svc.disable(user_id=uid1, actor="actor")
    assert disabled["status"] == "disabled"
    # Role should remain system_admin
    assert disabled["role"] == Role.SYSTEM_ADMIN.value


@pytest.mark.unit
def test_activate_pending_rejected(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="sub-1",
        email="a@example.org",
        name="A",
    )
    user_id = record["user_id"]
    with pytest.raises(users_svc.UserError):
        users_svc.activate(user_id=user_id, role=Role.PENDING, actor="actor")


@pytest.mark.unit
def test_count_active_admins(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    assert users_svc.count_active_admins() == 0
    # Activate one admin
    record1 = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="admin-1",
        email="admin1@example.org",
        name="Admin One",
    )
    uid1 = record1["user_id"]
    users_svc.activate(user_id=uid1, role=Role.SYSTEM_ADMIN, actor="actor")
    assert users_svc.count_active_admins() == 1
    # Activate second admin
    record2 = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="admin-2",
        email="admin2@example.org",
        name="Admin Two",
    )
    uid2 = record2["user_id"]
    users_svc.activate(user_id=uid2, role=Role.SYSTEM_ADMIN, actor="actor")
    assert users_svc.count_active_admins() == 2
    # Disable one admin
    users_svc.disable(user_id=uid1, actor="actor")
    assert users_svc.count_active_admins() == 1
