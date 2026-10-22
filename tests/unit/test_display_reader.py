"""Unit tests for :mod:`ovid2.display.reader`."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ovid2.core import events as ev
from ovid2.display.bus import JsonlEventBus
from ovid2.display.reader import follow_events, latest_event_log, read_events


@pytest.mark.unit
def test_read_events_skips_malformed_lines(tmp_path: Path) -> None:
    """Broken JSON lines are logged and skipped, valid ones yielded."""
    log = tmp_path / "events.jsonl"
    valid = {"v": 1, "kind": "log", "ts": "2026-07-07T10:00:00Z", "instance_id": "x"}
    log.write_text(
        json.dumps(valid) + "\n" + "this is not json\n" + json.dumps(valid) + "\n",
        encoding="utf-8",
    )
    records = list(read_events(log))
    assert len(records) == 2


@pytest.mark.unit
def test_follow_events_yields_new_writes(tmp_path: Path) -> None:
    """Events written after the reader starts are surfaced."""
    log = tmp_path / "events.jsonl"

    def _writer() -> None:
        with JsonlEventBus(log) as bus:
            time.sleep(0.1)
            for i in range(3):
                bus.emit(
                    ev.log_event(
                        instance_id="20260707-101112-abc123",
                        level="info",
                        message=f"msg-{i}",
                    )
                )
                time.sleep(0.05)
            bus.emit(ev.instance_finished(instance_id="20260707-101112-abc123", success=True))

    t = threading.Thread(target=_writer)
    t.start()

    try:
        seen = list(follow_events(log, poll_interval=0.05))
    finally:
        t.join(timeout=5)

    kinds = [r["kind"] for r in seen]
    assert kinds[-1] == "instance.finished"
    assert kinds.count("log") == 3


@pytest.mark.unit
def test_latest_event_log_returns_none_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``latest_event_log`` returns ``None`` when the work dir has no logs."""
    monkeypatch.setattr("ovid2.display.reader.WORK_DIR", tmp_path)
    assert latest_event_log() is None


@pytest.mark.unit
def test_latest_event_log_finds_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``latest_event_log`` returns the most recently modified log."""
    monkeypatch.setattr("ovid2.display.reader.WORK_DIR", tmp_path)
    old = tmp_path / "events-20260101-000000-aaaaaa.jsonl"
    new = tmp_path / "events-20260202-000000-bbbbbb.jsonl"
    old.write_text("{}\n")
    time.sleep(0.02)
    new.write_text("{}\n")
    result = latest_event_log()
    assert result is not None
    assert result.name == new.name
