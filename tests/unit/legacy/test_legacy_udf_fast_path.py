"""Legacy date / time UDF fast paths equal the ``dateutil`` originals.

``format_date`` / ``format_time`` answer canonical ISO values without
``dateutil``; the vectorised wrapper evaluates each distinct value once
and caches across batches. Both must be invisible: for every input the
result must equal the original ``dateutil``-based function.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from ovid2.legacy import udfs

pytestmark = pytest.mark.unit


def _dateutil_date(val: Any) -> str | None:
    from dateutil import parser as dp

    try:
        return str(udfs.legacy_strftime(dp.parse(val), "%Y-%m-%d"))
    except Exception:
        return None


def _dateutil_time(val: Any) -> str:
    from dateutil import parser as dp

    try:
        return str(dp.parse(val).strftime("%H:%M"))
    except Exception:
        return ""


def _inputs(n: int) -> list[Any]:
    rnd = random.Random(42)
    fixed: list[Any] = [
        None,
        "",
        " ",
        "bad",
        "2026-02-30",
        "2026-02-29",
        "2024-02-29",
        "2026-02-28 24:00",
        "2026-02-28 23:60",
        "0201-12-18",
        "0999-01-01",
        "1000-01-01",
        "1937-06-01 12:06:00.000",
        "2026-01-02T03:04",
        "2026-01-02 3:04",
        "2026-01-02 03:04:05.1234567",
        "2026-01-02 03:04:05.123+05:00",
        " 2026-01-02",
        "2026-01-02 ",
        "0000-01-01",
        "2026-13-01",
        "2026-00-10",
        "2026-01-00",
        "2026-01-02 03:04:05.",
        "2026-01-02 12",
        "2026-1-2",
        "2026-01-02 03:04:60",
        "2026-01-02 03:04:59.999999",
        "9999-12-31 23:59",
        "\uff12\uff10\uff12\uff16-01-02",
        "2026-01-02 03:04:05Z",
        20260102,
        1.5,
        True,
    ]
    digits = "0123456789"
    for _ in range(n):
        y = "".join(rnd.choice(digits) for _ in range(4))
        mo = rnd.choice(["00", "01", "02", "09", "12", "13", f"{rnd.randint(1, 12):02d}"])
        d = rnd.choice(["00", "01", "28", "29", "30", "31", "32", f"{rnd.randint(1, 31):02d}"])
        tail = rnd.choice(
            [
                "",
                " 00:00",
                f" {rnd.randint(0, 25):02d}:{rnd.randint(0, 61):02d}",
                f"T{rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 61):02d}",
                f" {rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 59):02d}.{rnd.randint(0, 999):03d}",
                " 1:02",
                "+01:00",
            ]
        )
        fixed.append(f"{y}-{mo}-{d}{tail}")
    return fixed


def test_format_date_and_time_fast_path_match_dateutil() -> None:
    bad = []
    for v in _inputs(30_000):
        if udfs.format_date(v) != _dateutil_date(v) or udfs.format_time(v) != _dateutil_time(v):
            bad.append(
                (v, udfs.format_date(v), _dateutil_date(v), udfs.format_time(v), _dateutil_time(v))
            )
    assert not bad, bad[:5]


def test_vectorised_wrapper_dedups_but_keeps_values() -> None:
    import pandas as pd

    calls: list[Any] = []

    def fn(v: Any) -> Any:
        calls.append(v)
        return None if v is None else f"<{v}>"

    captured: dict[str, Any] = {}

    def fake_pandas_udf(f: Any, _rt: Any) -> Any:
        captured["f"] = f
        return f

    import ovid2.core.engines.dsl as dsl

    orig = dsl.pandas_udf
    dsl.pandas_udf = fake_pandas_udf  # type: ignore[assignment]
    try:
        apply = udfs._vectorised_string_udf(fn)
    finally:
        dsl.pandas_udf = orig  # type: ignore[assignment]
    series = pd.Series(["a", "b", "a", None, float("nan"), "a", 1, 1.0, True], dtype=object)
    out = list(apply(series))
    assert out == ["<a>", "<b>", "<a>", None, None, "<a>", "<1>", "<1.0>", "<True>"]
    assert calls.count("a") == 1


@pytest.mark.parametrize("fn_name", ["format_date", "format_time"])
def test_duckdb_native_iso_fast_path_matches_python(fn_name: str) -> None:
    """On DuckDB canonical ISO values are answered in SQL; every value equals the Python UDF."""
    pytest.importorskip("duckdb")
    import pyarrow as pa

    from ovid2.core.engines import dsl
    from ovid2.core.engines.duckdb import DuckDBEngine

    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    try:
        values = [v for v in _inputs(5_000) if v is None or isinstance(v, str)]
        frame = eng.get_session().from_arrow(pa.table({"v": pa.array(values, pa.string())}))
        udf = getattr(udfs.legacy_udfs("acme"), fn_name)
        sql = frame.select(udf(dsl.col("v")).alias("o")).sql_query()
        assert "regexp_full_match" in sql  # native fast path in use
        got = frame.select(udf(dsl.col("v")).alias("o")).to_arrow().column(0).to_pylist()
        ref = getattr(udfs, fn_name)
        assert got == [ref(v) for v in values]
        # Non-string input keeps the exact Python rendering (UDF only).
        ints = eng.wrap(eng.connection().sql("SELECT 20260102::BIGINT AS v"))
        out = ints.select(udf(dsl.col("v")).alias("o")).to_arrow().column(0).to_pylist()
        assert out == [ref(20260102)]
    finally:
        eng.stop_session()
        dsl.set_active_engine(None)


@pytest.mark.parametrize("fn_name", ["format_date", "format_time", "get_valid_zip5"])
def test_vectorised_wrapper_is_cloudpicklable(fn_name: str) -> None:
    """PySpark cloudpickles the UDF body to ship it to executors.

    Regression: capturing the ``lru_cache`` wrapper in the closure failed with
    "Can't pickle <functools._lru_cache_wrapper …>: it's not the same object
    as ovid2.legacy.udfs.format_date", so every legacy formatter failed on
    PySpark.
    """
    import pandas as pd

    cloudpickle = pytest.importorskip("pyspark.cloudpickle")
    import ovid2.core.engines.dsl as dsl

    captured: dict[str, Any] = {}

    def fake_pandas_udf(f: Any, _rt: Any) -> Any:
        captured["f"] = f
        return f

    orig = dsl.pandas_udf
    dsl.pandas_udf = fake_pandas_udf  # type: ignore[assignment]
    try:
        udfs._vectorised_string_udf(getattr(udfs, fn_name))
    finally:
        dsl.pandas_udf = orig  # type: ignore[assignment]
    restored = cloudpickle.loads(cloudpickle.dumps(captured["f"]))
    ref = getattr(udfs, fn_name)
    values = ["2026-01-02", "01/02/2026 13:45", "32601-1234", None]
    assert list(restored(pd.Series(values, dtype=object))) == [ref(v) for v in values]
