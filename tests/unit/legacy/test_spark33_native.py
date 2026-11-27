"""Native Spark 3.3 CSV inference / rendering equals the exact Python fold.

:mod:`ovid2.legacy.spark33_native` decides column types in SQL and renders
typed cells natively. The oracle is the reference implementation in
:mod:`ovid2.legacy.spark33_infer` (``infer_type`` folded over the column in
order, ``render`` per cell). Columns are fuzzed from value pools covering
every class boundary (int / long / beyond-long, decimals, Java double
forms, timestamps incl. invalid calendar dates and time-only values,
booleans, year-like integers, whitespace, NaN / Inf) and shuffled, so
any order dependence the native path does not detect shows up.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.legacy import spark33_infer, spark33_native

pytestmark = pytest.mark.unit

_POOLS: dict[str, list[str | None]] = {
    "int": ["0", "-0", "+5", "42", "-2147483648", "2147483647", "0007", "123"],
    "year": ["2020", "1999", "+2021", "000123", "0000"],
    "long": ["2147483648", "-9223372036854775808", "9223372036854775807", "30000000000"],
    "big": [
        "9223372036854775808",
        "9300000000000000000",
        "-99999999999999999999",
        "123456789012345678901234567890",
        "1" * 39,
    ],
    "dec": ["1.5", "-0.25", ".5", "+3.14159", "0.0", "100.000"],
    "double": ["5.", "1e10", "1.5E-3", " 7 ", "2.5d", "NaN", "Infinity", "-Inf", "0x1p3"],
    "ts": [
        "2020-01-02",
        "2026-06-05 12:30:00",
        "2026-06-05T23:59:59.5",
        "2026-06-05 01:02:03.123456",
        "1000-01-01",
    ],
    "ts_odd": ["2020-02-30", "2020-13-01", "2020-01-02 24:00:00", "10:30", "T12:30", "2020-1-2"],
    "bool": ["true", "FALSE", "True"],
    "str": ["abc", "12-AB", "123 Main St", "x1", "--", "N/A", "1.2.3"],
    "empty": [None, ""],
}

_MIXES: list[tuple[str, ...]] = [
    ("int",),
    ("int", "year"),
    ("int", "long"),
    ("int", "long", "big"),
    ("big",),
    ("long", "big"),
    ("int", "dec"),
    ("big", "dec"),
    ("double",),
    ("int", "double"),
    ("ts",),
    ("ts", "year"),
    ("ts", "int"),
    ("ts", "dec"),
    ("ts", "ts_odd"),
    ("ts_odd",),
    ("bool",),
    ("bool", "int"),
    ("str",),
    ("str", "int"),
    ("empty",),
    ("year",),
]


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "2GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _columns(seed: int, rows: int = 60) -> list[list[str | None]]:
    rnd = random.Random(seed)
    cols: list[list[str | None]] = []
    for mix in _MIXES:
        pool = [v for name in mix for v in _POOLS[name]] + _POOLS["empty"]
        cols.append([rnd.choice(pool) for _ in range(rows)])
    return cols


def _frame(engine: DuckDBEngine, cols: list[list[str | None]]) -> Any:
    import pyarrow as pa

    table = pa.table({f"c{i}": pa.array(c, pa.string()) for i, c in enumerate(cols)})
    return engine.get_session().from_arrow(table)


@pytest.mark.parametrize("seed", range(12))
def test_native_types_equal_fold(engine: DuckDBEngine, seed: int) -> None:
    cols = _columns(seed)
    frame = _frame(engine, cols)
    native = spark33_infer._infer_column_types(
        frame.toDF(*[f"_c{i}" for i in range(len(cols))]), len(cols)
    )
    expected = [spark33_infer.infer_type(c) for c in cols]
    assert native == expected


@pytest.mark.parametrize("seed", range(6))
def test_native_render_equals_python(engine: DuckDBEngine, seed: int) -> None:
    cols = _columns(seed + 100)
    frame = _frame(engine, cols)
    got = frame.toDF(*[f"k{i}" for i in range(len(cols))])
    rendered = spark33_infer.infer_and_render(got, None).to_arrow()
    for i, c in enumerate(cols):
        t = spark33_infer.infer_type(c)
        want = list(c) if t == ("string",) else [spark33_infer.render(v, t) for v in c]
        assert rendered.column(i).to_pylist() == want, (i, t)


def test_every_type_renders_natively_against_oracle(engine: DuckDBEngine) -> None:
    """Each typed render (incl. values the SQL path hands to Python) matches ``render``."""
    cases: list[tuple[tuple[Any, ...], list[str | None]]] = [
        (("int",), ["0", "-0", "+5", "0007", None, ""]),
        (("long",), ["30000000000", "-9223372036854775808", "+0012"]),
        (("decimal", 30, 0), ["123456789012345678901234567890", "-0", "+0001", "1" * 30]),
        (
            ("double",),
            ["1.5", "-0", "0.0", "5.", "1e10", " 7 ", "NaN", "-Inf", "12345678.9", "0.0001"],
        ),
        (("timestamp",), ["2020-01-02", "2026-06-05T23:59:59.5", "2026-06-05 01:02:03.123456"]),
        (("boolean",), ["true", "FALSE", "True", None]),
    ]
    import pyarrow as pa

    for t, values in cases:
        frame = engine.get_session().from_arrow(pa.table({"v": pa.array(values, pa.string())}))
        items = spark33_native.render_items(engine, [t], ["v"])
        out = frame.select_native(items).to_arrow().column(0).to_pylist()
        assert out == [spark33_infer.render(v, t) for v in values], t


def test_native_switch_off_uses_fold(engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_DUCKDB_NATIVE_TEXT", "0")
    cols = _columns(7)
    frame = _frame(engine, cols).toDF(*[f"_c{i}" for i in range(len(cols))])
    assert spark33_native.native_engine(frame) is None
    assert spark33_infer._infer_column_types(frame, len(cols)) == [
        spark33_infer.infer_type(c) for c in cols
    ]


def test_order_dependent_mix_falls_back() -> None:
    """Year-like integers with timestamps depend on row order: the native path defers."""
    mask = (1 << spark33_native.TS) | (1 << spark33_native.YEAR)
    assert spark33_native.decide(mask) is None
    assert spark33_infer.infer_type(["2020-01-02", "2020"]) == ("timestamp",)
    assert spark33_infer.infer_type(["2020", "2020-01-02"]) == ("string",)
