"""Unit tests for the shared run-of-items aggregate."""

from __future__ import annotations

import pytest

from ovid2.core import events as ev
from ovid2.display.renderers._run_state import (
    CHECK_ITEMS,
    TABLE_ITEMS,
    RunAggregate,
    is_item_event,
    pick_item_kind,
)

_INSTANCE = "20260707-101112-abc123"


def _records(events: list[ev.Event]) -> list[dict]:
    return [e.to_json_dict() for e in events]


@pytest.mark.unit
def test_pick_item_kind_detects_checks() -> None:
    """A stream with a check event resolves to the check config."""
    records = _records(
        [
            ev.instance_started(instance_id=_INSTANCE, pipeline="site"),
            ev.check_started(instance_id=_INSTANCE, check_id="dc_1_01", name="X", part="1"),
        ]
    )
    assert pick_item_kind(records) is CHECK_ITEMS


@pytest.mark.unit
def test_pick_item_kind_defaults_to_tables() -> None:
    """With no item event seen, the default is the ETL/table config."""
    records = _records([ev.instance_started(instance_id=_INSTANCE, pipeline="site")])
    assert pick_item_kind(records) is TABLE_ITEMS


@pytest.mark.unit
def test_is_item_event() -> None:
    assert is_item_event("table.completed")
    assert is_item_event("check.failed")
    assert not is_item_event("instance.started")
    assert not is_item_event("fix.applied")


@pytest.mark.unit
def test_table_aggregate_counts_and_fixes() -> None:
    """ETL runs count tables and track the fixer chain."""
    agg = RunAggregate(item_kind=TABLE_ITEMS)
    for event in [
        ev.instance_started(instance_id=_INSTANCE, pipeline="site"),
        ev.table_started(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="demographic"
        ),
        ev.fix_applied(
            instance_id=_INSTANCE,
            pipeline="site",
            batch_id="20260101",
            table="demographic",
            fix="patid_orphans",
            current_fix=1,
            total_fixes=3,
        ),
        ev.table_completed(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="demographic"
        ),
        ev.table_started(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="encounter"
        ),
        ev.table_failed(
            instance_id=_INSTANCE,
            pipeline="site",
            batch_id="20260101",
            table="encounter",
            error="boom",
        ),
    ]:
        agg.apply(event.to_json_dict())

    assert agg.items_ok == 1
    assert agg.items_failed == 1
    assert agg.current_item == "encounter"
    # fix scratch was cleared on the completed/failed transitions
    assert agg.fix == ""
    assert any("encounter" in e for e in agg.recent_errors)


@pytest.mark.unit
def test_check_aggregate_exception_counts_as_failed() -> None:
    """A completed check with status='exception' counts as non-OK."""
    agg = RunAggregate(item_kind=CHECK_ITEMS)
    for event in [
        ev.instance_started(instance_id=_INSTANCE),
        ev.check_started(instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1"),
        ev.check_completed(
            instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1", status="passed"
        ),
        ev.check_started(instance_id=_INSTANCE, check_id="dc_1_02", name="Race", part="1"),
        ev.check_completed(
            instance_id=_INSTANCE,
            check_id="dc_1_02",
            name="Race",
            part="1",
            status="exception",
            exception_count=4,
        ),
        ev.check_failed(
            instance_id=_INSTANCE, check_id="dc_2_01", name="Enc", part="2", error="crash"
        ),
    ]:
        agg.apply(event.to_json_dict())

    assert agg.items_ok == 1
    assert agg.items_failed == 2  # exception + operator failure
    assert agg.current_group == "2"


@pytest.mark.unit
def test_check_aggregate_ignores_fix_events() -> None:
    """Health runs never emit fixes; a stray fix event is a no-op."""
    agg = RunAggregate(item_kind=CHECK_ITEMS)
    agg.apply(
        ev.fix_applied(
            instance_id=_INSTANCE,
            pipeline="x",
            batch_id="b",
            table="t",
            fix="noop",
            current_fix=1,
            total_fixes=1,
        ).to_json_dict()
    )
    assert agg.fix == ""
    assert agg.items_ok == 0
    assert agg.items_failed == 0


@pytest.mark.unit
def test_instance_finished_sets_finish_kind() -> None:
    agg = RunAggregate(item_kind=TABLE_ITEMS)
    agg.apply(ev.instance_finished(instance_id=_INSTANCE, success=False).to_json_dict())
    assert agg.finished is True
    assert agg.finish_kind == "failure"
