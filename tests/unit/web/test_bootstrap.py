"""Unit tests for the first-admin bootstrap helper (todo 19).

Exercises :func:`ovid2.web.routes.auth._maybe_bootstrap_admin` directly
against a temp web DB (via the ``isolated_xdg`` fixture). Driving the full
HTTP callback is unnecessary — the helper is the unit of behaviour the plan
gates on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.web import db as web_db
from ovid2.web.auth.roles import Role
from ovid2.web.models import SettingsAuth
from ovid2.web.routes import auth as auth_routes
from ovid2.web.services import users_svc

pytestmark = pytest.mark.unit


def _upsert(email: str) -> dict[str, Any]:
    """Upsert a fresh IdP user with the given email and return the record."""
    return users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=f"sub-{email}",
        email=email,
        name="Test User",
    )


def test_allowlisted_first_login_becomes_system_admin(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = _upsert("admin@example.org")
    auth = SettingsAuth(enabled=True, bootstrap_admin_emails=["admin@example.org"])

    result = auth_routes._maybe_bootstrap_admin(record, auth)

    assert result["role"] == Role.SYSTEM_ADMIN.value
    assert result["status"] == "active"
    reloaded = users_svc.get_user(str(record["user_id"]))
    assert reloaded is not None
    assert reloaded["role"] == Role.SYSTEM_ADMIN.value
    assert reloaded["status"] == "active"


def test_allowlist_is_case_insensitive_and_stripped(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = _upsert("Admin@EXAMPLE.org")
    auth = SettingsAuth(enabled=True, bootstrap_admin_emails=["admin@example.org"])

    result = auth_routes._maybe_bootstrap_admin(record, auth)

    assert result["role"] == Role.SYSTEM_ADMIN.value
    assert result["status"] == "active"


def test_non_allowlisted_stays_pending(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = _upsert("stranger@example.org")
    auth = SettingsAuth(enabled=True, bootstrap_admin_emails=["admin@example.org"])

    result = auth_routes._maybe_bootstrap_admin(record, auth)

    assert result["role"] == Role.PENDING.value
    assert result["status"] == Role.PENDING.value
    assert users_svc.count_active_admins() == 0


def test_already_active_bootstrap_admin_relogin_is_noop(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = _upsert("admin@example.org")
    auth = SettingsAuth(enabled=True, bootstrap_admin_emails=["admin@example.org"])

    first = auth_routes._maybe_bootstrap_admin(record, auth)
    assert first["role"] == Role.SYSTEM_ADMIN.value

    reloaded = users_svc.get_user(str(record["user_id"]))
    assert reloaded is not None
    second = auth_routes._maybe_bootstrap_admin(reloaded, auth)

    assert second["role"] == Role.SYSTEM_ADMIN.value
    assert second["status"] == "active"
    assert users_svc.count_active_admins() == 1


def test_disabled_bootstrap_admin_not_reactivated(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    record = _upsert("admin@example.org")
    auth = SettingsAuth(enabled=True, bootstrap_admin_emails=["admin@example.org"])
    user_id = str(record["user_id"])

    # Promote, then add a second admin so the last-admin guard permits
    # disabling the first, then disable the first.
    auth_routes._maybe_bootstrap_admin(record, auth)
    other = _upsert("other-admin@example.org")
    users_svc.activate(str(other["user_id"]), Role.SYSTEM_ADMIN, actor="test")
    users_svc.disable(user_id, actor="test")

    disabled = users_svc.get_user(user_id)
    assert disabled is not None
    assert disabled["status"] == "disabled"

    result = auth_routes._maybe_bootstrap_admin(disabled, auth)

    assert result["status"] == "disabled"
    reloaded = users_svc.get_user(user_id)
    assert reloaded is not None
    assert reloaded["status"] == "disabled"


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
