"""Unit tests for :class:`ovid2.scheduler.batch_tracker.SqliteBatchTracker`."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ovid2.scheduler.batch_tracker import (
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    SqliteBatchTracker,
)


@pytest.fixture
def tracker(tmp_path: Path) -> SqliteBatchTracker:
    """Return a tracker backed by a temp SQLite file."""
    t = SqliteBatchTracker(tmp_path / "scheduler.db")
    t.initialize_tables()
    yield t
    t.close()


@pytest.mark.unit
def test_register_then_read_status(tracker: SqliteBatchTracker) -> None:
    """Registered batches surface with status NEW."""
    tracker.register_batch("site/20260101", "/data/site/20260101")
    assert tracker.get_batch_status("site/20260101") == STATUS_NEW


@pytest.mark.unit
def test_register_is_idempotent(tracker: SqliteBatchTracker) -> None:
    """Re-registering the same batch must not raise or corrupt state."""
    tracker.register_batch("site/20260101", "/data/site/20260101")
    tracker.register_batch("site/20260101", "/data/site/20260101")
    assert tracker.get_batch_status("site/20260101") == STATUS_NEW


@pytest.mark.unit
def test_get_batch_status_unknown(tracker: SqliteBatchTracker) -> None:
    """Unknown batches return ``None``."""
    assert tracker.get_batch_status("nope/xyz") is None


@pytest.mark.unit
def test_update_status_marks_transitions(tracker: SqliteBatchTracker) -> None:
    """Status transitions from NEW → PROCESSING → PROCESSED."""
    tracker.register_batch("site/20260101", "/data")
    tracker.update_status("site/20260101", STATUS_PROCESSING, timestamp_field="processing_start")
    assert tracker.get_batch_status("site/20260101") == STATUS_PROCESSING
    tracker.update_status("site/20260101", STATUS_PROCESSED, timestamp_field="processing_end")
    assert tracker.get_batch_status("site/20260101") == STATUS_PROCESSED


@pytest.mark.unit
def test_update_status_rejects_unknown_status(tracker: SqliteBatchTracker) -> None:
    """Non-whitelisted status strings raise ``ValueError``."""
    tracker.register_batch("site/1", "/data")
    with pytest.raises(ValueError, match="Invalid status"):
        tracker.update_status("site/1", "NOT_A_STATUS")


@pytest.mark.unit
def test_update_status_rejects_unknown_timestamp_field(tracker: SqliteBatchTracker) -> None:
    """Non-whitelisted timestamp fields raise ``ValueError``."""
    tracker.register_batch("site/1", "/data")
    with pytest.raises(ValueError, match="Invalid timestamp_field"):
        tracker.update_status("site/1", STATUS_PROCESSING, timestamp_field="drop_table")


@pytest.mark.unit
def test_get_ready_batches_respects_min_wait(tracker: SqliteBatchTracker) -> None:
    """Only NEW batches older than ``min_wait_minutes`` are returned."""
    tracker.register_batch("recent/1", "/data/recent/1")
    # Manually backdate a second batch by an hour so it's ready.
    old_iso = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    with sqlite3.connect(tracker.path) as raw:
        raw.execute(
            "INSERT INTO data_batch(batch_id, path, received_time, status) VALUES (?, ?, ?, ?)",
            ("old/1", "/data/old/1", old_iso, STATUS_NEW),
        )

    ready = tracker.get_ready_batches(min_wait_minutes=15)
    assert [row["batch_id"] for row in ready] == ["old/1"]


@pytest.mark.unit
def test_get_ready_batches_excludes_processed(tracker: SqliteBatchTracker) -> None:
    """Batches with status != NEW are never in the ready list."""
    tracker.register_batch("site/1", "/data")
    tracker.update_status("site/1", STATUS_ERROR)
    assert tracker.get_ready_batches(min_wait_minutes=0) == []


@pytest.mark.unit
def test_reset_to_new_clears_timestamps(tracker: SqliteBatchTracker) -> None:
    """``reset_to_new`` flips status back to NEW and clears both timestamps."""
    tracker.register_batch("site/reset", "/data")
    tracker.update_status("site/reset", STATUS_PROCESSING, timestamp_field="processing_start")
    tracker.update_status("site/reset", STATUS_ERROR, timestamp_field="processing_end")
    tracker.reset_to_new("site/reset")
    assert tracker.get_batch_status("site/reset") == STATUS_NEW
    row = tracker._conn.execute(
        "SELECT processing_start, processing_end FROM data_batch WHERE batch_id = ?",
        ("site/reset",),
    ).fetchone()
    assert row == (None, None)


@pytest.mark.unit
def test_reset_to_new_unknown_batch_is_noop(tracker: SqliteBatchTracker) -> None:
    """Resetting an unknown batch does not raise."""
    tracker.reset_to_new("does_not_exist")
    assert tracker.get_batch_status("does_not_exist") is None
