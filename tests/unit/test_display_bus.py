"""Unit tests for :class:`ovid2.display.bus.JsonlEventBus` and :class:`TeeEventBus`."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ovid2.core import events as ev
from ovid2.display.bus import JsonlEventBus, TeeEventBus


@pytest.mark.unit
def test_jsonl_bus_appends_one_line_per_event(tmp_path: Path) -> None:
    """Each emitted event becomes one JSON line."""
    log = tmp_path / "events.jsonl"
    with JsonlEventBus(log) as bus:
        bus.emit(ev.instance_started(instance_id="20260707-101112-abc123", pipeline="site"))
        bus.emit(
            ev.table_started(
                instance_id="20260707-101112-abc123",
                pipeline="site",
                batch_id="20260101",
                table="demographic",
            )
        )

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["kind"] == "instance.started"
    assert records[0]["v"] == 1
    assert records[1]["kind"] == "table.started"
    assert records[1]["pipeline"] == "site"
    assert records[1]["table"] == "demographic"


@pytest.mark.unit
def test_jsonl_bus_file_mode_is_0600(tmp_path: Path) -> None:
    """The event-log file is created with mode 0600."""
    log = tmp_path / "events.jsonl"
    with JsonlEventBus(log) as bus:
        bus.emit(ev.log_event(instance_id="20260707-101112-abc123", level="info", message="hi"))
    mode = stat.S_IMODE(os.stat(log).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.unit
def test_jsonl_bus_appends_to_existing_file(tmp_path: Path) -> None:
    """Re-opening the same path preserves existing lines."""
    log = tmp_path / "events.jsonl"
    with JsonlEventBus(log) as bus:
        bus.emit(ev.log_event(instance_id="20260707-101112-abc123", level="info", message="one"))
    with JsonlEventBus(log) as bus:
        bus.emit(ev.log_event(instance_id="20260707-101112-abc123", level="info", message="two"))
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


class _RecordingBus:
    def __init__(self) -> None:
        self.received: list[ev.Event] = []

    def emit(self, event: ev.Event) -> None:
        self.received.append(event)


@pytest.mark.unit
def test_tee_bus_forwards_to_every_downstream() -> None:
    """Every downstream sees every event."""
    a = _RecordingBus()
    b = _RecordingBus()
    tee = TeeEventBus(a, b)
    event = ev.log_event(instance_id="20260707-101112-abc123", level="info", message="hello")
    tee.emit(event)
    assert a.received == [event]
    assert b.received == [event]


@pytest.mark.unit
def test_tee_bus_isolates_downstream_failures() -> None:
    """A raising downstream does not stop delivery to the others."""

    class _Broken:
        def emit(self, _event: ev.Event) -> None:
            raise RuntimeError("nope")

    good = _RecordingBus()
    tee = TeeEventBus(_Broken(), good)
    tee.emit(ev.log_event(instance_id="20260707-101112-abc123", level="info", message="hi"))
    assert len(good.received) == 1
