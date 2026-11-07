"""Cross-engine parity: identical expressions on PySpark and DuckDB.

Every case builds the *same* frame and expression through each engine's
portable surface (``engine.create_typed_dataframe`` +
``engine.get_functions()`` / ``selectExpr``) and asserts the collected
rows match. Spark runs with the production session config
(``SPARK_CONFIG``: ANSI off, ``timeParserPolicy=LEGACY``). Skips when
no JVM is available (shared ``spark`` fixture).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

pytest.importorskip("duckdb")

pytestmark = pytest.mark.integration

_ROWS = [
    ("1", "2023-4-5", " a b ", "12.7", "M", "E11.9", "10:11"),
    ("2", "04/15/2023", "x", "abc", "fn", "I10", None),
    ("3", "bad", None, "-3.2", "UN", "", "1:2"),
    ("4", None, "", None, None, None, ""),
]
_SCHEMA = [(c, "string") for c in ("id", "d", "s", "n", "sex", "dx", "t")]


@pytest.fixture(scope="module")
def engines(spark: Any) -> Iterator[dict[str, Any]]:
    from ovid2.config.settings import SPARK_CONFIG
    from ovid2.core.engines.duckdb import DuckDBEngine
    from ovid2.core.engines.pyspark import PySparkEngine

    # Production SQL semantics (the shared fixture only pins ANSI).
    previous = spark.conf.get("spark.sql.legacy.timeParserPolicy")
    spark.conf.set(
        "spark.sql.legacy.timeParserPolicy", SPARK_CONFIG["spark.sql.legacy.timeParserPolicy"]
    )
    py = PySparkEngine()
    py._session = spark  # reuse the session-scoped fixture
    duck = DuckDBEngine(args={"threads": 2, "memory_limit": "2GB"})
    yield {"pyspark": py, "duckdb": duck}
    duck.stop_session()
    spark.conf.set("spark.sql.legacy.timeParserPolicy", previous)


def _norm(rows: list[Any]) -> list[tuple[Any, ...]]:
    out = []
    for r in rows:
        out.append(
            tuple(
                None
                if v is None
                else (
                    float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
                )
                for v in r
            )
        )
    return sorted(out, key=repr)


_CASES: dict[str, Callable[[Any, Any], Any]] = {
    "upper_trim": lambda fn, df: df.select(fn.upper(fn.trim(fn.col("s"))).alias("o")),
    "coalesce_lit": lambda fn, df: df.select(fn.coalesce(fn.col("s"), fn.lit("NI")).alias("o")),
    "when_chain": lambda fn, df: df.select(
        fn.when(fn.col("sex") == "M", "8507")
        .when(fn.col("sex") == "fn", "8532")
        .otherwise("0")
        .alias("o")
    ),
    "isin_not": lambda fn, df: df.filter(~fn.col("sex").isin("UN", "NI")).select("id"),
    "to_date_iso": lambda fn, df: df.select(
        fn.date_format(fn.to_date(fn.col("d"), "yyyy-MM-dd"), "yyyy-MM-dd").alias("o")
    ),
    "to_date_us": lambda fn, df: df.select(
        fn.date_format(fn.to_date(fn.col("d"), "MM/dd/yyyy"), "yyyy-MM-dd").alias("o")
    ),
    "to_date_noformat": lambda fn, df: df.select(
        fn.date_format(fn.to_date(fn.col("d")), "yyyy-MM-dd").alias("o")
    ),
    "to_double": lambda fn, df: df.select(fn.to_double(fn.col("n")).alias("o")),
    "cast_int": lambda fn, df: df.select(fn.col("n").cast("int").alias("o")),
    "num_compare": lambda fn, df: df.select((fn.col("n") > 0).alias("o")),
    "substring": lambda fn, df: df.select(fn.substring(fn.col("dx"), 1, 3).alias("o")),
    "regexp_replace": lambda fn, df: df.select(
        fn.regexp_replace(fn.col("dx"), "\\.", "").alias("o")
    ),
    "regexp_extract": lambda fn, df: df.select(
        fn.regexp_extract(fn.col("t"), "^(\\d+):", 1).alias("o")
    ),
    "concat_ws": lambda fn, df: df.select(fn.concat_ws("|", fn.col("id"), fn.col("s")).alias("o")),
    "concat_null": lambda fn, df: df.select(fn.concat(fn.col("id"), fn.col("s")).alias("o")),
    "length": lambda fn, df: df.select(fn.length(fn.col("s")).alias("o")),
    "map_from_dict": lambda fn, df: df.select(
        fn.map_from_dict({"M": "8507", "fn": "8532"}, fn.col("sex"), "0").alias("o")
    ),
    "create_map_lookup": lambda fn, df: df.select(
        fn.coalesce(
            fn.create_map(fn.lit("M"), fn.lit("Male"), fn.lit("fn"), fn.lit("Female"))[
                fn.col("sex")
            ],
            fn.col("sex"),
        ).alias("o")
    ),
    "create_map_large": lambda fn, df: df.select(
        fn.create_map([fn.lit(x) for i in range(300) for x in (f"E{i}.9", f"dx{i}")])[
            fn.col("dx")
        ].alias("o")
    ),
    "year": lambda fn, df: df.select(fn.year(fn.to_date(fn.col("d"), "yyyy-MM-dd")).alias("o")),
    "datediff": lambda fn, df: df.select(
        fn.datediff(fn.lit("2023-04-15"), fn.to_date(fn.col("d"), "yyyy-MM-dd")).alias("o")
    ),
    "count_group": lambda fn, df: df.groupBy("sex").agg(fn.count(fn.col("id")).alias("c")),
    "max_agg": lambda fn, df: df.agg(fn.max(fn.col("dx")).alias("m")),
    "select_expr": lambda fn, df: df.selectExpr(
        "coalesce(date_format(to_date(d, 'yyyy-MM-dd'), 'yyyy'), '') as y",
        "CASE WHEN dx RLIKE '^E' THEN 'endo' ELSE 'other' END as k",
        "cast(n as double) as nd",
    ),
    "filter_sql": lambda fn, df: df.filter("sex = 'M' OR n > 0").select("id"),
    "drop_dupes": lambda fn, df: df.select("sex").dropDuplicates(["sex"]),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_dsl_parity(engines: dict[str, Any], case: str) -> None:
    from ovid2.core.engines import dsl

    results = {}
    for name, eng in engines.items():
        with dsl.use_engine(eng):
            df = eng.create_typed_dataframe(_ROWS, _SCHEMA)
            results[name] = _norm(_CASES[case](eng.get_functions(), df).collect())
    assert results["duckdb"] == results["pyspark"], case
