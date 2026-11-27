"""DuckDB Python UDFs evaluate each distinct input once, with identical results.

Spark treats a UDF as deterministic unless ``asNondeterministic()`` is
called, so the DuckDB engine may evaluate a row UDF once per distinct
argument tuple (memo) and a pandas UDF once per distinct value in a
chunk (dictionary encoding). These tests pin that results are unchanged
(including NULLs and type-distinct keys such as ``1`` vs ``1.0`` vs
``True``), that calls are actually deduped, and that a non-deterministic
UDF is called for every row.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.core.engines.types import StringType

pytestmark = pytest.mark.unit


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _frame(engine: DuckDBEngine) -> Any:
    return engine.wrap(
        engine.connection().sql(
            "SELECT CASE WHEN i % 11 = 0 THEN NULL ELSE 'v' || (i % 50)::VARCHAR END AS a, "
            "(i % 3)::VARCHAR AS b FROM range(20000) r(i)"
        )
    )


def _counter() -> tuple[list[int], threading.Lock]:
    return [0], threading.Lock()


def test_row_udf_memo_keeps_values_and_dedupes_calls(engine: DuckDBEngine) -> None:
    calls, lock = _counter()

    def fn(a: str | None, b: str | None) -> str | None:
        with lock:
            calls[0] += 1
        return None if a is None else f"{a}|{b}"

    rows = (
        _frame(engine)
        .select(
            dsl.col("a"),
            dsl.col("b"),
            dsl.udf(fn, StringType())(dsl.col("a"), dsl.col("b")).alias("o"),
        )
        .relation.fetchall()
    )
    assert all(o == (None if a is None else f"{a}|{b}") for a, b, o in rows)
    assert calls[0] < 2000  # 150 distinct tuples (+ per-thread repeats), not 20 000


def test_non_deterministic_udf_runs_per_row(engine: DuckDBEngine) -> None:
    calls, lock = _counter()

    def fn(a: str | None) -> str | None:
        with lock:
            calls[0] += 1
        return a

    nd = dsl.udf(fn, StringType()).asNondeterministic()
    _frame(engine).select(nd(dsl.col("a")).alias("o")).relation.fetchall()
    assert calls[0] == 20000


def test_pandas_udf_dedups_per_chunk(engine: DuckDBEngine) -> None:
    import pandas as pd

    seen: list[int] = []

    def fn(s: pd.Series) -> pd.Series:
        seen.append(len(s))
        return s.map(lambda v: None if v is None else v.upper())

    rows = (
        _frame(engine)
        .select(dsl.col("a"), dsl.pandas_udf(fn, StringType())(dsl.col("a")).alias("o"))
        .relation.fetchall()
    )
    assert all(o == (None if a is None else a.upper()) for a, o in rows)
    assert sum(seen) < 20000


def test_memo_key_is_type_tagged() -> None:
    from ovid2.core.engines._duck_udf import DuckUdf

    impl = DuckUdf(lambda v: repr(v), StringType())._python()
    assert [impl(1), impl(1.0), impl(True), impl(1)] == ["1", "1.0", "True", "1"]
