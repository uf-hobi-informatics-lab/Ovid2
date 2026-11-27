"""Legacy phase 1 runs tables one at a time, on every engine.

``ovid2.legacy.runner.run_phase1`` pins the engine-consistent contract:
every table runs exactly once, in the given order, never two at once, on
the calling thread; a cancel stops the loop. The per-table source-text
map (``csv_roundtrip.remember_source_text``) stays thread-isolated.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from ovid2.legacy import csv_roundtrip, runner

pytestmark = pytest.mark.unit


class _FakeEngine:
    def __init__(self, session_is_spark: bool) -> None:
        self.session_is_spark = session_is_spark


@pytest.mark.parametrize("is_spark", [True, False])
def test_tables_run_serially_in_order_on_the_main_thread(is_spark: bool) -> None:
    main = threading.current_thread().name
    seen: list[tuple[str, str]] = []
    active = {"now": 0, "peak": 0}

    def one(table: str) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        seen.append((table, threading.current_thread().name))
        active["now"] -= 1

    tables = [f"t{i}" for i in range(6)]
    runner.run_phase1(_FakeEngine(is_spark), tables, one)
    assert [t for t, _ in seen] == tables
    assert {name for _, name in seen} == {main}
    assert active["peak"] == 1


def test_interrupt_stops_the_loop() -> None:
    started: list[str] = []

    def one(table: str) -> None:
        started.append(table)
        if table == "t1":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runner.run_phase1(_FakeEngine(False), [f"t{i}" for i in range(5)], one)
    assert started == ["t0", "t1"]


def test_source_text_is_isolated_per_thread() -> None:
    results: dict[str, Any] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        view, text = object(), f"text-{name}"
        csv_roundtrip.remember_source_text(view, text)  # type: ignore[arg-type]
        barrier.wait()
        results[name] = csv_roundtrip._source_text_map().get(id(view))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"a": "text-a", "b": "text-b"}
