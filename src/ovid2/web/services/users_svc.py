"""User store service for web authentication and authorization (W6).

Manages the ``web_users`` table in the web-tier SQLite database.
Provides CRUD operations for user records retrieved from identity providers
(IDPs) and role/status management with last-admin guardrails.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ovid2.web import db as web_db
from ovid2.web.auth.roles import Role


class UserError(ValueError):
    """Raised when a user operation is invalid or violates security constraints."""


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a user record dictionary."""
    return {
        "user_id": row["user_id"],
        "issuer": row["issuer"],
        "subject": row["subject"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def upsert_from_idp(
    issuer: str,
    subject: str,
    email: str | None,
    name: str | None,
) -> dict[str, Any]:
    """Insert or update a user from IDP data.

    On first sight: INSERT a new row with role='pending', status='pending',
    generated user_id, and timestamps set to now.
    On returning identity: UPDATE email/name (if provided), updated_at,
    and last_login_at but NEVER downgrade an existing role or status.

    Args:
        issuer: The IDP issuer string.
        subject: The subject identifier from the IDP.
        email: Optional email address from the IDP.
        name: Optional display name from the IDP.

    Returns:
        The resulting user record as a dictionary.
    """
    now = datetime.now(timezone.utc).isoformat()
    user_id = secrets.token_urlsafe(16)

    with web_db.connect() as conn:
        # Try to find existing user by issuer/subject
        existing = conn.execute(
            "SELECT * FROM web_users WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()

        if existing is None:
            # Insert new user
            conn.execute(
                """
                INSERT INTO web_users (
                    user_id, issuer, subject, email, name, role, status,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    issuer,
                    subject,
                    email,
                    name,
                    Role.PENDING.value,
                    Role.PENDING.value,
                    now,
                    now,
                    now,
                ),
            )
            user_id_out = user_id
        else:
            # COALESCE(?, col) keeps role/status untouched (never downgrade on
            # re-login) and updates email/name only when the IdP sent a value,
            # so the UPDATE stays static/parameterised (AGENTS.md forbids
            # dynamic f-string SQL).
            user_id_out = existing["user_id"]

            conn.execute(
                """
                UPDATE web_users
                SET updated_at = ?,
                    last_login_at = ?,
                    email = COALESCE(?, email),
                    name = COALESCE(?, name)
                WHERE user_id = ?
                """,
                (now, now, email, name, user_id_out),
            )

        # Return the resulting user record
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id_out,)).fetchone()
        assert row is not None
        return _row_to_record(row)


def get_user(user_id: str) -> dict[str, Any] | None:
    """Get a user by user_id.

    Args:
        user_id: The user identifier.

    Returns:
        The user record as a dictionary, or None if not found.
    """
    with web_db.connect() as conn:
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        return _row_to_record(row) if row is not None else None


def get_by_identity(issuer: str, subject: str) -> dict[str, Any] | None:
    """Get a user by issuer and subject.

    Args:
        issuer: The IDP issuer string.
        subject: The subject identifier from the IDP.

    Returns:
        The user record as a dictionary, or None if not found.
    """
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_users WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        return _row_to_record(row) if row is not None else None


def list_users(status: str | None = None) -> list[dict[str, Any]]:
    """List users, optionally filtered by status.

    Args:
        status: Optional status filter ('pending', 'active', 'disabled').

    Returns:
        List of user record dictionaries.
    """
    with web_db.connect() as conn:
        if status is None:
            rows = conn.execute("SELECT * FROM web_users").fetchall()
        else:
            rows = conn.execute("SELECT * FROM web_users WHERE status = ?", (status,)).fetchall()
        return [_row_to_record(row) for row in rows]


def activate(user_id: str, role: Role, actor: str) -> dict[str, Any]:
    """Activate a user with a specific role.

    Sets status='active' and role=role.value.
    Rejects Role.PENDING with UserError.
    Enforces last-admin guard for role changes away from system_admin.

    Args:
        user_id: The user identifier.
        role: The role to assign (cannot be Role.PENDING).
        actor: Identifier of the actor performing the action.

    Returns:
        The updated user record as a dictionary.

    Raises:
        UserError: If role is Role.PENDING or if the operation would violate
                  the last-admin guard.
    """
    if role == Role.PENDING:
        raise UserError("Cannot activate user with Role.PENDING")

    with web_db.connect() as conn:
        # Get current user state
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"User not found: {user_id}")

        current_role = row["role"]
        current_status = row["status"]

        # Check last-admin guard: if user is active system_admin and would be
        # the only active admin after this change, reject
        if (
            current_role == Role.SYSTEM_ADMIN.value
            and current_status == "active"
            and role != Role.SYSTEM_ADMIN
        ):
            # Would be changing role away from system_admin
            active_admins = conn.execute(
                """
                SELECT COUNT(*) FROM web_users
                WHERE role = ? AND status = ?
                """,
                (Role.SYSTEM_ADMIN.value, "active"),
            ).fetchone()[0]
            if active_admins <= 1:
                raise UserError("Cannot change role of the last active system administrator")

        # Update user
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE web_users
            SET role = ?, status = ?, updated_at = ?, last_login_at = ?
            WHERE user_id = ?
            """,
            (role.value, "active", now, now, user_id),
        )

        # Audit the change
        web_db.ensure_schema()
        web_db.audit(
            actor=actor,
            action="user.activate",
            target=user_id,
            detail_json=f'{{"role": "{role.value}"}}',
        )

        # Return updated record
        updated_row = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert updated_row is not None
        return _row_to_record(updated_row)


def set_role(user_id: str, role: Role, actor: str) -> dict[str, Any]:
    """Change a user's role.

    Rejects Role.PENDING.
    Enforces last-admin guard: cannot reduce active system_admin count to zero.

    Args:
        user_id: The user identifier.
        role: The new role (cannot be Role.PENDING).
        actor: Identifier of the actor performing the action.

    Returns:
        The updated user record as a dictionary.

    Raises:
        UserError: If role is Role.PENDING or if the operation would violate
                  the last-admin guard.
    """
    if role == Role.PENDING:
        raise UserError("Cannot set user role to Role.PENDING")

    with web_db.connect() as conn:
        # Get current user state
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"User not found: {user_id}")

        current_role = row["role"]
        current_status = row["status"]

        # Check last-admin guard: if user is active system_admin and would be
        # the only active admin after this change, reject
        if (
            current_role == Role.SYSTEM_ADMIN.value
            and current_status == "active"
            and role != Role.SYSTEM_ADMIN
        ):
            # Would be changing role away from system_admin
            active_admins = conn.execute(
                """
                SELECT COUNT(*) FROM web_users
                WHERE role = ? AND status = ?
                """,
                (Role.SYSTEM_ADMIN.value, "active"),
            ).fetchone()[0]
            if active_admins <= 1:
                raise UserError("Cannot change role of the last active system administrator")

        # Update user
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE web_users
            SET role = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (role.value, now, user_id),
        )

        # Audit the change
        web_db.ensure_schema()
        web_db.audit(
            actor=actor,
            action="user.set_role",
            target=user_id,
            detail_json=f'{{"role": "{role.value}"}}',
        )

        # Return updated record
        updated_row = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert updated_row is not None
        return _row_to_record(updated_row)


def disable(user_id: str, actor: str) -> dict[str, Any]:
    """Disable a user.

    Sets status='disabled'.
    Enforces last-admin guard: cannot disable the last active system_admin.

    Args:
        user_id: The user identifier.
        actor: Identifier of the actor performing the action.

    Returns:
        The updated user record as a dictionary.

    Raises:
        UserError: If the operation would violate the last-admin guard.
    """
    with web_db.connect() as conn:
        # Get current user state
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"User not found: {user_id}")

        current_role = row["role"]
        current_status = row["status"]

        # Check last-admin guard: if user is active system_admin and would be
        # the only active admin after disabling, reject
        if current_role == Role.SYSTEM_ADMIN.value and current_status == "active":
            active_admins = conn.execute(
                """
                SELECT COUNT(*) FROM web_users
                WHERE role = ? AND status = ?
                """,
                (Role.SYSTEM_ADMIN.value, "active"),
            ).fetchone()[0]
            if active_admins <= 1:
                raise UserError("Cannot disable the last active system administrator")

        # Update user
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE web_users
            SET status = ?, updated_at = ?
            WHERE user_id = ?
            """,
            ("disabled", now, user_id),
        )

        # Audit the change
        web_db.ensure_schema()
        web_db.audit(
            actor=actor,
            action="user.disable",
            target=user_id,
            detail_json='{"status": "disabled"}',
        )

        # Return updated record
        updated_row = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert updated_row is not None
        return _row_to_record(updated_row)


def enable(user_id: str, actor: str) -> dict[str, Any]:
    """Enable a disabled user.

    Sets status='active'.

    Args:
        user_id: The user identifier.
        actor: Identifier of the actor performing the action.

    Returns:
        The updated user record as a dictionary.
    """
    with web_db.connect() as conn:
        # Get current user state
        row = conn.execute("SELECT * FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserError(f"User not found: {user_id}")

        # Update user
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE web_users
            SET status = ?, updated_at = ?
            WHERE user_id = ?
            """,
            ("active", now, user_id),
        )

        # Audit the change
        web_db.ensure_schema()
        web_db.audit(
            actor=actor,
            action="user.enable",
            target=user_id,
            detail_json='{"status": "active"}',
        )

        # Return updated record
        updated_row = conn.execute(
            "SELECT * FROM web_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert updated_row is not None
        return _row_to_record(updated_row)


def count_active_admins() -> int:
    """Count active system administrators.

    Returns:
        Number of users with role='system_admin' and status='active'.
    """
    with web_db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM web_users
            WHERE role = ? AND status = ?
            """,
            (Role.SYSTEM_ADMIN.value, "active"),
        ).fetchone()
        return row[0] if row is not None else 0
