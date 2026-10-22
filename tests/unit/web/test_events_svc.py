"""Unit tests for ``ovid2.web.services.events_svc``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.services import events_svc


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    """Append events as JSONL lines to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


@pytest.mark.unit
def test_read_page_returns_events(isolated_xdg: Path) -> None:
    """First page returns everything in the log with a non-zero cursor."""
    instance = "20260101-100000-abcdef"
    _write_events(
        events_svc.event_log_path(instance),
        [
            {"v": 1, "kind": "instance.started", "ts": "2026-01-01T10:00:00Z"},
            {"v": 1, "kind": "table.started", "ts": "2026-01-01T10:00:01Z", "table": "demographic"},
        ],
    )
    page = events_svc.read_page(instance)
    assert page.instance_id == instance
    assert len(page.events) == 2
    assert page.next_cursor > 0
    assert page.finished is False


@pytest.mark.unit
def test_read_page_incremental_by_cursor(isolated_xdg: Path) -> None:
    """Subsequent poll with ``after=next_cursor`` only returns new events."""
    instance = "20260101-100000-abcdef"
    path = events_svc.event_log_path(instance)
    _write_events(path, [{"v": 1, "kind": "instance.started", "ts": "t0"}])
    first = events_svc.read_page(instance)
    _write_events(path, [{"v": 1, "kind": "instance.finished", "ts": "t1"}])
    second = events_svc.read_page(instance, after=first.next_cursor)
    assert [e["kind"] for e in second.events] == ["instance.finished"]
    assert second.finished is True


@pytest.mark.unit
def test_read_page_no_log_yet(isolated_xdg: Path) -> None:
    """Missing event log returns an empty page with the same cursor."""
    page = events_svc.read_page("does-not-exist", after=42)
    assert page.events == []
    assert page.next_cursor == 42


@pytest.mark.unit
def test_read_page_skips_malformed_lines(isolated_xdg: Path) -> None:
    """Truncated / non-JSON lines are dropped, not raised."""
    instance = "20260101-100000-abcdef"
    path = events_svc.event_log_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"v": 1, "kind": "log", "ts": "t0"}) + "\n")
    page = events_svc.read_page(instance)
    assert len(page.events) == 1
    assert page.events[0]["kind"] == "log"


@pytest.mark.unit
def test_read_page_limit_caps_events(isolated_xdg: Path) -> None:
    """``limit`` is honoured; remaining events come back on the next call."""
    instance = "20260101-100000-abcdef"
    path = events_svc.event_log_path(instance)
    _write_events(
        path,
        [{"v": 1, "kind": "log", "ts": f"t{i}"} for i in range(5)],
    )
    first = events_svc.read_page(instance, limit=2)
    assert len(first.events) == 2
    second = events_svc.read_page(instance, after=first.next_cursor, limit=10)
    assert len(second.events) == 3


@pytest.mark.unit
def test_latest_instance_id(isolated_xdg: Path) -> None:
    """Returns the most recently written event log's instance id."""
    for instance in ("20260101-100000-aaaaaa", "20260102-100000-bbbbbb"):
        _write_events(
            events_svc.event_log_path(instance),
            [{"v": 1, "kind": "log", "ts": instance}],
        )
    latest = events_svc.latest_instance_id()
    assert latest in {"20260101-100000-aaaaaa", "20260102-100000-bbbbbb"}
