"""Unit tests for the Rich TUI renderer's state + panel building."""

from __future__ import annotations

import pytest

from ovid2.core import events as ev
from ovid2.display.renderers._run_state import CHECK_ITEMS, TABLE_ITEMS
from ovid2.display.renderers.tui import _State, _build_panel

_INSTANCE = "20260707-101112-abc123"


def _apply(state: _State, events: list[ev.Event]) -> None:
    for event in events:
        state.apply(event.to_json_dict())


def _panel_text(state: _State) -> str:
    from rich.console import Console

    console = Console(record=True, width=100)
    console.print(_build_panel(state))
    return console.export_text()


@pytest.mark.unit
def test_state_autodetects_etl_run() -> None:
    """A leading instance.started does not lock the kind to ETL prematurely."""
    state = _State()
    _apply(
        state,
        [
            ev.instance_started(instance_id=_INSTANCE, pipeline="site"),
            ev.table_started(
                instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="demographic"
            ),
        ],
    )
    assert state.aggregate.item_kind is TABLE_ITEMS
    assert state.aggregate.pipeline == "site"


@pytest.mark.unit
def test_state_autodetects_health_run() -> None:
    """A run whose first item event is a check resolves to the check config."""
    state = _State()
    _apply(
        state,
        [
            ev.instance_started(instance_id=_INSTANCE),
            ev.check_started(instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1"),
        ],
    )
    assert state.aggregate.item_kind is CHECK_ITEMS


@pytest.mark.unit
def test_panel_renders_check_labels_and_counts() -> None:
    """The health panel shows check labels, part grouping and check counts."""
    state = _State()
    _apply(
        state,
        [
            ev.instance_started(instance_id=_INSTANCE),
            ev.check_started(instance_id=_INSTANCE, check_id="dc_1_02", name="Race", part="1"),
            ev.check_completed(
                instance_id=_INSTANCE,
                check_id="dc_1_02",
                name="Race",
                part="1",
                status="exception",
                exception_count=2,
            ),
        ],
    )
    text = _panel_text(state)
    assert "check" in text
    assert "dc_1_02" in text
    assert "part" in text
    assert "checks" in text
    assert "1 failed" in text


@pytest.mark.unit
def test_panel_renders_table_labels_and_fixes() -> None:
    """The ETL panel shows table labels, a stage row and the fix row."""
    state = _State()
    _apply(
        state,
        [
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
        ],
    )
    text = _panel_text(state)
    assert "demographic" in text
    assert "patid_orphans" in text
    assert "1/3" in text
    assert "tables" in text


@pytest.mark.unit
def test_items_failed_property_before_any_event() -> None:
    """A fresh state reports no failures and is not finished."""
    state = _State()
    assert state.items_failed == 0
    assert state.finished is False


@pytest.mark.unit
def test_rendering_before_item_event_does_not_lock_kind() -> None:
    """Reading .aggregate after only instance.started must stay unresolved.

    Regression: a provisional render must not lock the run kind to ETL
    before the first check.* event, otherwise a health run is mislabelled.
    """
    state = _State()
    state.apply(ev.instance_started(instance_id=_INSTANCE).to_json_dict())
    # Provisional snapshot renders as ETL but must not commit the kind.
    assert state.aggregate.item_kind is TABLE_ITEMS
    # A subsequent check event still resolves the run kind to checks.
    state.apply(
        ev.check_started(instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1").to_json_dict()
    )
    assert state.aggregate.item_kind is CHECK_ITEMS
