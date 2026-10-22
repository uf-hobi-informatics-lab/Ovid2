"""Phase-A admin storage-account settings + admin-state tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web import settings as web_settings
from ovid2.web.models import ConnectionWrite, GlobalSettings, SettingsAdmin
from ovid2.web.services import connections_svc, settings_svc


@pytest.mark.unit
def test_admin_storage_default_is_none() -> None:
    assert GlobalSettings().admin.storage_account is None


@pytest.mark.unit
def test_admin_storage_helper_none_when_unset(isolated_xdg: Path) -> None:
    settings_svc.invalidate_cache()
    assert web_settings.admin_storage_connection() is None


@pytest.mark.unit
def test_admin_storage_round_trip_with_known_connection(isolated_xdg: Path) -> None:
    connections_svc.write_connection(
        "admin_disc",
        ConnectionWrite(type="disc", args={"host_path": str(isolated_xdg)}),
        actor="test",
        create_only=True,
    )
    settings_svc.save_settings(
        GlobalSettings(admin=SettingsAdmin(storage_account="admin_disc")),
        actor="test",
    )
    persisted = settings_svc.get_settings()
    assert persisted.admin.storage_account == "admin_disc"
    assert web_settings.admin_storage_connection() == "admin_disc"


@pytest.mark.unit
def test_admin_storage_rejects_unknown_connection(isolated_xdg: Path) -> None:
    with pytest.raises(settings_svc.SettingsError, match="unknown connection"):
        settings_svc.save_settings(
            GlobalSettings(admin=SettingsAdmin(storage_account="nope")),
            actor="test",
        )


@pytest.mark.unit
def test_admin_storage_empty_string_accepted(isolated_xdg: Path) -> None:
    settings_svc.save_settings(
        GlobalSettings(admin=SettingsAdmin(storage_account="")),
        actor="test",
    )
    assert settings_svc.get_settings().admin.storage_account is None


@pytest.mark.unit
def test_ensure_schema_creates_admin_state_tables(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    web_db.ensure_schema()
    with web_db.connect() as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "web_watch_cursors" in names
    assert "web_pipeline_runs" in names


@pytest.mark.unit
def test_watch_cursor_pk_is_pipeline_input(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_watch_cursors "
            "(pipeline, input_address, last_seen_batch, last_checked_at) "
            "VALUES (?, ?, ?, ?)",
            ("p1", "in", "2024_01", "now"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO web_watch_cursors "
                "(pipeline, input_address, last_seen_batch, last_checked_at) "
                "VALUES (?, ?, ?, ?)",
                ("p1", "in", "2024_02", "now"),
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
