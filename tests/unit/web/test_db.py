"""Unit tests for ``ovid2.web.db``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ovid2.web import db as web_db


@pytest.mark.unit
def test_ensure_schema_creates_web_tables(isolated_xdg: Path) -> None:
    """Both ``web_jobs`` and ``web_audit`` are created idempotently."""
    web_db.ensure_schema()
    # Second call must not raise.
    web_db.ensure_schema()

    with web_db.connect() as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"web_jobs", "web_audit"}.issubset(tables)


@pytest.mark.unit
def test_audit_appends_row(isolated_xdg: Path) -> None:
    """``audit`` writes a timestamped row with the correct fields."""
    web_db.ensure_schema()
    web_db.audit(
        actor="test",
        action="run.submit",
        target="demo:20260101",
        detail_json='{"tables": ["all"]}',
    )
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT actor, action, target, detail_json FROM web_audit"
        ).fetchone()
    assert row["actor"] == "test"
    assert row["action"] == "run.submit"
    assert row["target"] == "demo:20260101"
    assert row["detail_json"] == '{"tables": ["all"]}'


@pytest.mark.unit
def test_connect_uses_wal(isolated_xdg: Path) -> None:
    """The connection helper enables WAL journal mode."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


@pytest.mark.unit
def test_ensure_schema_is_additive_over_existing_db(isolated_xdg: Path) -> None:
    """Web tables coexist with an existing scheduler ``data_batch`` table."""
    from ovid2.web.settings import web_db_path

    # Pre-create a scheduler-style table to prove additivity.
    path = web_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE data_batch (batch_id TEXT PRIMARY KEY, path TEXT, "
        "received_time TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO data_batch VALUES ('B1', '/x', '2026-01-01', 'NEW')")
    conn.commit()
    conn.close()

    web_db.ensure_schema()

    with web_db.connect() as conn:
        row = conn.execute("SELECT batch_id FROM data_batch").fetchone()
    assert row["batch_id"] == "B1"
