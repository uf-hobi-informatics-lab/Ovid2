"""``ovid2.core.table_runner`` — one table at a time, on every engine."""

from __future__ import annotations

import threading

import pytest

from ovid2.core.table_runner import TABLE_CONCURRENCY, run_tables

pytestmark = pytest.mark.unit


def test_concurrency_is_pinned_to_one() -> None:
    assert TABLE_CONCURRENCY == 1


def test_tables_run_in_cdm_order_never_overlapping() -> None:
    seen: list[str] = []
    active = {"now": 0, "peak": 0}
    main = threading.current_thread().name
    threads: set[str] = set()

    def _run(table: str) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        threads.add(threading.current_thread().name)
        seen.append(table)
        active["now"] -= 1

    result = run_tables(["z", "a", "m", "b"], _run)
    assert seen == ["z", "a", "m", "b"] == result.order
    assert result.succeeded == seen
    assert active["peak"] == 1
    assert threads == {main}


def test_failures_are_recorded_and_run_continues() -> None:
    failures: list[str] = []

    def _run(table: str) -> None:
        if table == "bad":
            raise ValueError("boom")

    result = run_tables(["a", "bad", "c"], _run, on_failure=lambda t, e: failures.append(t))
    assert failures == ["bad"] and set(result.failed) == {"bad"}
    assert result.succeeded == ["a", "c"]


def test_fail_fast_stops_after_first_failure() -> None:
    seen: list[str] = []
    hooked: list[str] = []

    def _run(table: str) -> None:
        seen.append(table)
        if table == "first":
            raise ValueError("boom")

    result = run_tables(
        ["first", "second", "third"],
        _run,
        fail_fast=True,
        on_failure=lambda t, e: hooked.append(t),
    )
    assert seen == ["first"] and set(result.failed) == {"first"} and hooked == ["first"]


def test_keyboard_interrupt_propagates() -> None:
    seen: list[str] = []

    def _run(table: str) -> None:
        seen.append(table)
        if table == "b":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_tables(["a", "b", "c"], _run)
    assert seen == ["a", "b"]


def test_engine_error_after_cancel_stops_the_run() -> None:
    """A cancel the engine reports as a plain error must not start more tables."""
    from ovid2.core import cancellation

    started: list[str] = []

    def _run(table: str) -> None:
        started.append(table)
        if table == "b":
            cancellation.request_cancel()
            raise RuntimeError("Query interrupted")

    try:
        with pytest.raises(KeyboardInterrupt):
            run_tables(["a", "b", "c", "d"], _run)
    finally:
        cancellation.reset()
    assert started == ["a", "b"]
