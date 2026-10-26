"""Unit tests for web_users and web_sessions schema in ``ovid2.web.db``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ovid2.web import db as web_db


@pytest.mark.unit
def test_ensure_schema_creates_auth_tables(isolated_xdg: Path) -> None:
    """Both ``web_users`` and ``web_sessions`` are created idempotently."""
    web_db.ensure_schema()
    # Second call must not raise.
    web_db.ensure_schema()

    with web_db.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {"web_users", "web_sessions"}.issubset(tables)


@pytest.mark.unit
def test_ensure_schema_creates_auth_indexes(isolated_xdg: Path) -> None:
    """Expected indexes on web_users and web_sessions are created."""
    web_db.ensure_schema()

    with web_db.connect() as conn:
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
    # web_users indexes
    assert "ix_web_users_status" in indexes
    assert "ix_web_users_email" in indexes
    # web_sessions indexes
    assert "ix_web_sessions_user_id" in indexes
    assert "ix_web_sessions_expires_at" in indexes


@pytest.mark.unit
def test_ensure_schema_is_idempotent_for_auth_tables(isolated_xdg: Path) -> None:
    """Calling ensure_schema() twice does not cause errors."""
    web_db.ensure_schema()
    try:
        web_db.ensure_schema()  # Should not raise
    except Exception as e:
        pytest.fail(f"ensure_schema() raised on second call: {e}")


@pytest.mark.unit
def test_web_users_unique_issuer_subject_constraint(isolated_xdg: Path) -> None:
    """Duplicate (issuer, subject) pairs raise IntegrityError."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        # Insert first row
        conn.execute(
            """
            INSERT INTO web_users (
                user_id, issuer, subject, email, name, role, status,
                created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "user1",
                "issuer1",
                "subject1",
                "user1@example.com",
                "User One",
                "admin",
                "active",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
        conn.commit()
        # Attempt to insert duplicate (issuer, subject) should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO web_users (
                    user_id, issuer, subject, email, name, role, status,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "user2",
                    "issuer1",
                    "subject1",
                    "user2@example.com",
                    "User Two",
                    "user",
                    "pending",
                    "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    None,
                ),
            )
            conn.commit()
