"""Unit tests for the plain-log renderer."""

from __future__ import annotations

import io

import pytest

from ovid2.core import events as ev
from ovid2.display.renderers.plain_log import PlainLogBus, render_stream


@pytest.mark.unit
def test_plain_log_bus_writes_one_line_per_event() -> None:
    """Each emitted event writes one line to the stream."""
    stream = io.StringIO()
    bus = PlainLogBus(stream)
    bus.emit(
        ev.table_started(
            instance_id="20260707-101112-abc123",
            pipeline="site",
            batch_id="20260101",
            table="demographic",
        )
    )
    bus.emit(
        ev.fix_applied(
            instance_id="20260707-101112-abc123",
            pipeline="site",
            batch_id="20260101",
            table="demographic",
            fix="patid_orphans",
            current_fix=1,
            total_fixes=3,
        )
    )
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 2
    assert "[table.started]" in lines[0]
    assert "site" in lines[0]
    assert "demographic" in lines[0]
    assert "patid_orphans" in lines[1]
    assert "1/3" in lines[1]


@pytest.mark.unit
def test_render_stream_from_iterable() -> None:
    """``render_stream`` accepts an iterable of dicts."""
    stream = io.StringIO()
    records = [
        {
            "v": 1,
            "kind": "warning",
            "ts": "2026-07-07T10:00:00Z",
            "instance_id": "20260707-101112-abc123",
            "message": "watch out",
        }
    ]
    render_stream(records, stream=stream)
    assert "watch out" in stream.getvalue()


@pytest.mark.unit
def test_plain_log_formats_check_events() -> None:
    """Health check events render with check id, part and status."""
    stream = io.StringIO()
    bus = PlainLogBus(stream)
    bus.emit(
        ev.check_started(
            instance_id="20260707-101112-abc123",
            check_id="dc_1_02",
            name="Race",
            part="1",
        )
    )
    bus.emit(
        ev.check_completed(
            instance_id="20260707-101112-abc123",
            check_id="dc_1_02",
            name="Race",
            part="1",
            status="exception",
            exception_count=4,
        )
    )
    bus.emit(
        ev.check_failed(
            instance_id="20260707-101112-abc123",
            check_id="dc_2_01",
            name="Enc",
            part="2",
            error="operator crashed",
        )
    )
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 3
    assert "check=dc_1_02" in lines[0]
    assert "part=1" in lines[0]
    assert "status=exception" in lines[1]
    assert "exceptions=4" in lines[1]
    assert "FAILED: operator crashed" in lines[2]
