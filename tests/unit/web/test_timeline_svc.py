"""Unit tests for ``ovid2.web.services.timeline_svc``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.services import events_svc, timeline_svc


def _write_events(instance: str, events: list[dict[str, object]]) -> None:
    """Write JSONL events to the instance's log file."""
    path = events_svc.event_log_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


@pytest.mark.unit
def test_timeline_aggregates_stage_counts(isolated_xdg: Path) -> None:
    """Stage counters increment once per matching event."""
    instance = "20260101-100000-aaaaaa"
    _write_events(
        instance,
        [
            {"v": 1, "kind": "instance.started", "ts": "t0"},
            {
                "v": 1,
                "kind": "table.started",
                "ts": "t1",
                "stage": "format",
                "table": "demographic",
            },
            {
                "v": 1,
                "kind": "table.completed",
                "ts": "t2",
                "stage": "format",
                "table": "demographic",
                "elapsed_ms": 42,
            },
            {"v": 1, "kind": "table.started", "ts": "t3", "stage": "format", "table": "encounter"},
            {
                "v": 1,
                "kind": "table.failed",
                "ts": "t4",
                "stage": "format",
                "table": "encounter",
                "error": "boom",
            },
            {"v": 1, "kind": "instance.finished", "ts": "t5", "extra": {"success": False}},
        ],
    )
    timeline = timeline_svc.build_timeline(instance)
    assert timeline.started_at == "t0"
    assert timeline.ended_at == "t5"
    assert timeline.success is False
    assert timeline.stage_counts["format"].started == 2
    assert timeline.stage_counts["format"].completed == 1
    assert timeline.stage_counts["format"].failed == 1


@pytest.mark.unit
def test_timeline_table_statuses(isolated_xdg: Path) -> None:
    """Each table is folded to its final status."""
    instance = "20260101-100000-bbbbbb"
    _write_events(
        instance,
        [
            {"v": 1, "kind": "table.started", "ts": "t1", "table": "demographic"},
            {
                "v": 1,
                "kind": "table.completed",
                "ts": "t2",
                "table": "demographic",
                "elapsed_ms": 100,
            },
            {"v": 1, "kind": "table.started", "ts": "t3", "table": "encounter"},
            {"v": 1, "kind": "table.failed", "ts": "t4", "table": "encounter", "error": "kaboom"},
            {"v": 1, "kind": "instance.finished", "ts": "t5", "extra": {"success": False}},
        ],
    )
    timeline = timeline_svc.build_timeline(instance)
    tables = {t.table: t for t in timeline.tables}
    assert tables["demographic"].status == "completed"
    assert tables["demographic"].elapsed_ms == 100
    assert tables["encounter"].status == "failed"
    assert tables["encounter"].error == "kaboom"


@pytest.mark.unit
def test_timeline_counts_fixes(isolated_xdg: Path) -> None:
    """Fix events are recorded and classified by status."""
    instance = "20260101-100000-cccccc"
    _write_events(
        instance,
        [
            {
                "v": 1,
                "kind": "fix.applied",
                "ts": "t1",
                "table": "demographic",
                "fix": "future_date_to_null",
            },
            {
                "v": 1,
                "kind": "fix.failed",
                "ts": "t2",
                "table": "demographic",
                "fix": "hash_token",
                "error": "no salt",
            },
            {"v": 1, "kind": "fix.skipped", "ts": "t3", "table": "encounter", "fix": "orphans"},
            {"v": 1, "kind": "instance.finished", "ts": "t4", "extra": {"success": True}},
        ],
    )
    timeline = timeline_svc.build_timeline(instance)
    statuses = {(f.fix, f.status) for f in timeline.fixes}
    assert ("future_date_to_null", "applied") in statuses
    assert ("hash_token", "failed") in statuses
    assert ("orphans", "skipped") in statuses


@pytest.mark.unit
def test_timeline_counts_warnings_and_errors(isolated_xdg: Path) -> None:
    """Warning / error events increment the top-level counters."""
    instance = "20260101-100000-dddddd"
    _write_events(
        instance,
        [
            {"v": 1, "kind": "warning", "ts": "t0", "message": "hmm"},
            {"v": 1, "kind": "error", "ts": "t1", "message": "boom"},
            {"v": 1, "kind": "log", "ts": "t2", "level": "warning", "message": "hmm2"},
            {"v": 1, "kind": "log", "ts": "t3", "level": "error", "message": "boom2"},
            {"v": 1, "kind": "instance.finished", "ts": "t4", "extra": {"success": True}},
        ],
    )
    timeline = timeline_svc.build_timeline(instance)
    assert timeline.warning_count == 2
    assert timeline.error_count == 2


@pytest.mark.unit
def test_timeline_missing_log(isolated_xdg: Path) -> None:
    """Missing log yields an empty timeline."""
    timeline = timeline_svc.build_timeline("does-not-exist")
    assert timeline.tables == []
    assert timeline.fixes == []
    assert timeline.events_seen == 0


@pytest.mark.unit
def test_timeline_folds_check_events(isolated_xdg: Path) -> None:
    """``check.*`` events fold into per-check summaries."""
    instance = "20260101-100000-cccccc"
    _write_events(
        instance,
        [
            {"v": 1, "kind": "instance.started", "ts": "t0"},
            {
                "v": 1,
                "kind": "check.started",
                "ts": "t1",
                "check_id": "1.01",
                "check_name": "DC_1_01",
                "part": "part1",
            },
            {
                "v": 1,
                "kind": "check.completed",
                "ts": "t2",
                "check_id": "1.01",
                "check_name": "DC_1_01",
                "part": "part1",
                "check_status": "exception",
                "exception_count": 2,
                "metric_count": 5,
            },
            {
                "v": 1,
                "kind": "check.started",
                "ts": "t3",
                "check_id": "1.04",
                "check_name": "DC_1_04",
                "part": "part1",
            },
            {
                "v": 1,
                "kind": "check.failed",
                "ts": "t4",
                "check_id": "1.04",
                "check_name": "DC_1_04",
                "part": "part1",
                "error": "boom",
            },
            {"v": 1, "kind": "instance.finished", "ts": "t5", "extra": {"success": True}},
        ],
    )
    timeline = timeline_svc.build_timeline(instance)
    by_id = {c.check_id: c for c in timeline.checks}
    assert by_id["1.01"].status == "exception"
    assert by_id["1.01"].exception_count == 2
    assert by_id["1.01"].metric_count == 5
    assert by_id["1.04"].status == "error"
    assert by_id["1.04"].error == "boom"
