"""Cross-engine parity: identical expressions on every engine.

PySpark is the reference; DuckDB always runs; Snowpark runs when
``OVID2_TEST_SNOWPARK_ENGINE=<engine config>`` names a live engine
(``-m real_connection``). Every name in
:data:`ovid2.core.engines.surface.FUNCTIONS` used here must give the same
rows on every engine (engine universality, ``adr-engine-universality.md``).


Every case builds the *same* frame and expression through each engine's
portable surface (``engine.create_typed_dataframe`` +
``engine.get_functions()`` / ``selectExpr``) and asserts the collected
rows match. Spark runs with the production session config
(``SPARK_CONFIG``: ANSI off, ``timeParserPolicy=LEGACY``). Skips when
no JVM is available (shared ``spark`` fixture).
"""

from __future__ import annotations

import os
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
    out: dict[str, Any] = {"pyspark": py, "duckdb": duck}
    snow_name = os.environ.get("OVID2_TEST_SNOWPARK_ENGINE", "").strip()
    snow = None
    if snow_name:
        from ovid2.core.loaders import load_engine

        snow = load_engine(snow_name)
        warehouse = os.environ.get("OVID2_TEST_SNOWPARK_WAREHOUSE", "").strip()
        if warehouse:
            snow.native().use_warehouse(warehouse)
        databases = [
            d for d in os.environ.get("OVID2_TEST_SNOWPARK_DATABASE", "").split(",") if d.strip()
        ]
        snow.prepare_workspace(databases)
        out["snowpark"] = snow
    yield out
    duck.stop_session()
    if snow is not None:
        snow.stop_session()
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
    # > 512 string entries: Snowpark switches from CASE to a UDF lookup.
    "create_map_huge": lambda fn, df: df.select(
        fn.coalesce(
            fn.create_map([fn.lit(x) for i in range(2000) for x in (f"E{i}.9", f"dx{i}")])[
                fn.col("dx")
            ],
            fn.lit("none"),
        ).alias("o")
    ),
    # Java-only regex (``\p{L}``, ``\b``): Snowpark falls back to a Python UDF.
    "rlike_unicode_class": lambda fn, df: df.select(
        fn.col("s").rlike("\\p{L}").alias("l"), fn.col("dx").rlike("\\bE11\\b").alias("b")
    ),
    "regexp_replace_unicode_class": lambda fn, df: df.select(
        fn.regexp_replace(fn.col("s"), "\\p{L}", "[$0]").alias("o")
    ),
    "regexp_extract_word_boundary": lambda fn, df: df.select(
        fn.regexp_extract(fn.col("dx"), "\\b([A-Z])(\\d+)\\b", 2).alias("o")
    ),
    # Replacing an existing column keeps its position (Spark semantics).
    "with_column_replace_keeps_order": lambda fn, df: (
        df.select("id", "sex", "dx").withColumn("SEX", fn.lower(fn.col("sex"))).toDF("a", "b", "c")
    ),
    # Integer-valued map (geocoding RUCA lookups), small (CASE) and huge (UDF).
    "create_map_int_values": lambda fn, df: df.select(
        fn.coalesce(
            fn.create_map(fn.lit("E11.9"), fn.lit(1), fn.lit("I10"), fn.lit(10))[fn.col("dx")].cast(
                "string"
            ),
            fn.lit(""),
        ).alias("o")
    ),
    "create_map_huge_int_values": lambda fn, df: df.select(
        fn.coalesce(
            fn.create_map([fn.lit(x) for i in range(2000) for x in (f"E{i}.9", i % 10)])[
                fn.col("dx")
            ].cast("string"),
            fn.lit(""),
        ).alias("o")
    ),
    # Doubles rendered as text: Java ``Double.toString`` (``56.0``, ``1.0E20``).
    "double_to_string": lambda fn, df: df.select(
        fn.col("n").cast("double").cast("string").alias("a"),
        (fn.col("n").cast("double") * fn.lit(1e19)).cast("string").alias("b"),
        fn.concat(fn.col("n").cast("float"), fn.lit("|")).alias("c"),
    ),
    "double_into_udf": lambda fn, df: df.select(
        fn.udf(lambda v: None if v is None else f"<{v}>", "string")(
            fn.col("n").cast("double")
        ).alias("o")
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
    # --- engine-universality surface -------------------------------------
    "udf_row": lambda fn, df: df.select(
        fn.udf(lambda v: None if v is None else v.strip().upper() + "!", "string")(
            fn.col("s")
        ).alias("o")
    ),
    "udf_float": lambda fn, df: df.select(
        fn.udf(
            lambda v: float(v) if v and v.replace(".", "", 1).lstrip("-").isdigit() else None,
            "double",
        )(fn.col("n")).alias("o")
    ),
    "pandas_udf": lambda fn, df: df.select(
        fn.pandas_udf(lambda s: s.map(lambda v: None if v is None else f"<{v}>"), "string")(
            fn.col("sex")
        ).alias("o")
    ),
    "row_number_window": lambda fn, df: df.select(
        "id",
        fn.row_number()
        .over(fn.Window.partitionBy(fn.col("sex")).orderBy(fn.col("id").desc()))
        .alias("rn"),
    ),
    "rank_dense_rank": lambda fn, df: df.select(
        "id",
        fn.rank().over(fn.Window.orderBy(fn.col("sex"))).alias("r"),
        fn.dense_rank().over(fn.Window.orderBy(fn.col("sex"))).alias("dr"),
    ),
    "lag_lead": lambda fn, df: df.select(
        "id",
        fn.lag("id").over(fn.Window.orderBy("id")).alias("p"),
        fn.lead("id").over(fn.Window.orderBy("id")).alias("n"),
    ),
    "running_count": lambda fn, df: df.select(
        "id",
        fn.count(fn.col("id"))
        .over(fn.Window.orderBy("id").rowsBetween(fn.Window.unboundedPreceding, 0))
        .alias("c"),
    ),
    "lpad_instr": lambda fn, df: df.select(
        fn.lpad(fn.col("id"), 3, "0").alias("p"), fn.instr(fn.col("dx"), ".").alias("i")
    ),
    "isnan": lambda fn, df: df.select(fn.isnan(fn.col("n")).alias("o")),
    "floor_ceil": lambda fn, df: df.select(
        fn.floor(fn.col("n")).alias("f"), fn.ceil(fn.col("n")).alias("c")
    ),
    "greatest_least": lambda fn, df: df.select(
        fn.greatest(fn.col("id"), fn.lit("2")).alias("g"),
        fn.least(fn.col("id"), fn.lit("2")).alias("l"),
    ),
    "fillna": lambda fn, df: df.fillna("Z").select("s", "sex"),
    "dropna": lambda fn, df: df.dropna(subset=["s"]).select("id"),
    "subtract": lambda fn, df: df.select("sex").subtract(
        df.filter(fn.col("sex") == "M").select("sex")
    ),
    "collect_rdd_mapping": lambda fn, df: df.sparkSession.createDataFrame(
        sorted(dict(df.select("id", "sex").rdd.map(lambda r: (r[0], r[1])).collect()).items()),
        ["k", "v"],
    ),
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
    if "snowpark" in results:
        assert results["snowpark"] == results["pyspark"], case
