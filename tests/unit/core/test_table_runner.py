"""``ovid2.core.table_runner`` — two-phase, largest-first parallel tables."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from ovid2.core.table_runner import (
    order_largest_first,
    run_tables,
    split_reference_tables,
    supports_parallel_tables,
)

pytestmark = pytest.mark.unit


class _ParallelEngine:
    """Engine stub exposing the parallel-table protocol."""

    def __init__(self, concurrency: int) -> None:
        self._concurrency = concurrency
        self.worker_scopes = 0
        self.cancelled = False
        self._lock = threading.Lock()

    def table_concurrency(self, n_tables: int) -> int:
        return max(1, min(self._concurrency, n_tables))

    @contextlib.contextmanager
    def table_worker(self) -> Iterator[None]:
        with self._lock:
            self.worker_scopes += 1
        yield

    def cancel_running_queries(self) -> None:
        self.cancelled = True


class _SequentialEngine:
    """Spark / Snowpark stand-in: no parallel-table protocol."""


def test_order_largest_first_stable_unknown_last() -> None:
    tables = ["a", "b", "c", "d", "e"]
    sizes = {"b": 10, "c": 500, "e": 10}
    assert order_largest_first(tables, sizes) == ["c", "b", "e", "a", "d"]


def test_split_reference_tables_keeps_order() -> None:
    head, tail = split_reference_tables(
        ["demographic", "provider", "diagnosis", "encounter", "lab_result_cm"],
        ["encounter", "DEMOGRAPHIC", "provider"],
    )
    assert head == ["demographic", "provider", "encounter"]
    assert tail == ["diagnosis", "lab_result_cm"]


def test_sequential_engine_keeps_cdm_order() -> None:
    seen: list[str] = []
    result = run_tables(
        ["z", "a", "m"],
        seen.append,
        engine=_SequentialEngine(),
        reference_tables=["m"],
        table_sizes=lambda ts: {"a": 100},
    )
    assert not supports_parallel_tables(_SequentialEngine())
    assert seen == ["z", "a", "m"] and result.concurrency == 1


def test_concurrency_one_is_sequential() -> None:
    seen: list[str] = []
    engine = _ParallelEngine(1)
    run_tables(["a", "b"], seen.append, engine=engine, table_sizes=lambda ts: {"b": 9})
    assert seen == ["a", "b"] and engine.worker_scopes == 0


def test_reference_tables_finish_before_any_other_starts() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _run(table: str) -> None:
        with lock:
            events.append(("start", table))
        time.sleep(0.05 if table in {"demographic", "encounter"} else 0.01)
        with lock:
            events.append(("end", table))

    engine = _ParallelEngine(4)
    result = run_tables(
        ["demographic", "diagnosis", "encounter", "vital", "procedures"],
        _run,
        engine=engine,
        reference_tables=["demographic", "encounter"],
        table_sizes=lambda ts: {"vital": 1, "procedures": 50, "diagnosis": 20},
    )
    # Phase A strictly sequential, in CDM order.
    assert events[:4] == [
        ("start", "demographic"),
        ("end", "demographic"),
        ("start", "encounter"),
        ("end", "encounter"),
    ]
    assert result.order == ["demographic", "encounter", "procedures", "diagnosis", "vital"]
    assert result.concurrency == 3
    assert engine.worker_scopes == 3  # only phase-B tables use worker cursors
    assert sorted(result.succeeded) == sorted(result.order)


def test_parallel_phase_starts_largest_first_and_overlaps() -> None:
    started: list[str] = []
    running = {"now": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Barrier(2, timeout=5)

    def _run(table: str) -> None:
        with lock:
            started.append(table)
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        if table in {"big", "mid"}:
            gate.wait()  # both must be in flight together
        with lock:
            running["now"] -= 1

    run_tables(
        ["small", "mid", "big", "tiny"],
        _run,
        engine=_ParallelEngine(2),
        table_sizes=lambda ts: {"small": 5, "mid": 50, "big": 500, "tiny": 1},
    )
    assert started[:2] == ["big", "mid"]
    assert running["peak"] == 2


def test_failures_are_recorded_and_run_continues() -> None:
    failures: list[str] = []

    def _run(table: str) -> None:
        if table == "bad":
            raise ValueError("boom")

    result = run_tables(
        ["a", "bad", "c"],
        _run,
        engine=_ParallelEngine(3),
        on_failure=lambda t, e: failures.append(t),
    )
    assert failures == ["bad"] and set(result.failed) == {"bad"}
    assert sorted(result.succeeded) == ["a", "c"]


def test_fail_fast_stops_scheduling_new_tables() -> None:
    seen: list[str] = []

    def _run(table: str) -> None:
        seen.append(table)
        if table == "first":
            raise ValueError("boom")

    result = run_tables(
        ["first", "second", "third"],
        _run,
        engine=_ParallelEngine(1),
        fail_fast=True,
    )
    assert seen == ["first"] and set(result.failed) == {"first"}


def test_size_lookup_failure_keeps_cdm_order() -> None:
    def _boom(tables: list[str]) -> dict[str, int]:
        raise PermissionError("denied")

    seen: list[str] = []
    lock = threading.Lock()

    def _run(table: str) -> None:
        with lock:
            seen.append(table)

    result = run_tables(["a", "b", "c"], _run, engine=_ParallelEngine(1), table_sizes=_boom)
    assert result.order == ["a", "b", "c"]


def test_keyboard_interrupt_cancels_and_propagates() -> None:
    engine = _ParallelEngine(2)

    def _run(table: str) -> None:
        if table == "a":
            raise KeyboardInterrupt
        time.sleep(0.01)

    with pytest.raises(KeyboardInterrupt):
        run_tables(["a", "b", "c", "d"], _run, engine=engine)
    assert engine.cancelled


def test_worker_threads_inherit_active_engine_context() -> None:
    from ovid2.core.engines import dsl

    marker: Any = object()
    seen: list[Any] = []
    lock = threading.Lock()

    def _run(table: str) -> None:
        with lock:
            seen.append(dsl._ACTIVE.get())

    with dsl.use_engine(marker):
        run_tables(["a", "b", "c"], _run, engine=_ParallelEngine(3))
    assert seen == [marker, marker, marker]
