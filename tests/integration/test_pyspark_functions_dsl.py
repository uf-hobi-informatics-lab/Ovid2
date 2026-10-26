"""Unit tests for the portable :class:`EngineFunctions` DSL extensions on
the PySpark engine (E-portability phase 1).

Exercises the new members (``concat``, ``concat_ws``, ``current_date``,
``max``, ``map_from_dict``, ``stable_hash``, ``rand``) plus the
DataFrame-shape helpers on :class:`BaseEngine`
(``union_by_name``, ``drop_duplicates``, ``with_column``) against a real
local Spark session. ``.collect()`` here is a *test* materialisation — it
lives under ``tests/`` and is outside the lazy-Spark contract's src walk.
"""

from __future__ import annotations

import pytest

from ovid2.core.engines.pyspark import PySparkEngine

pytestmark = pytest.mark.integration


@pytest.fixture()
def engine() -> PySparkEngine:
    """A PySparkEngine whose session is provided by the `spark` fixture.

    We don't build a session through the engine (that would spin up a
    fresh JVM); instead the tests read the functions adapter (which is
    engine-independent) and use the shared ``spark`` fixture's session
    for DataFrame construction.
    """
    return PySparkEngine()


def test_concat_and_concat_ws(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.createDataFrame([("a", "b")], ["x", "y"])
    out = df.select(
        f.concat(f.col("x"), f.col("y")).alias("cat"),
        f.concat_ws("-", f.col("x"), f.col("y")).alias("catws"),
    ).collect()
    assert out[0]["cat"] == "ab"
    assert out[0]["catws"] == "a-b"


def test_current_date_returns_a_date(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    import datetime

    f = engine.get_functions()
    df = spark.createDataFrame([(1,)], ["n"])
    out = df.select(f.current_date().alias("today")).collect()
    assert isinstance(out[0]["today"], datetime.date)


def test_max_aggregate(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.createDataFrame([(1,), (5,), (3,)], ["n"])
    out = df.agg(f.max(f.col("n")).alias("m")).collect()
    assert out[0]["m"] == 5


def test_map_from_dict_hit_and_default(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.createDataFrame([("M",), ("F",), ("?",)], ["sex"])
    mapping = {"M": "8507", "F": "8532"}
    out = df.select(f.map_from_dict(mapping, f.col("sex"), default="0").alias("concept")).collect()
    assert [r["concept"] for r in out] == ["8507", "8532", "0"]


def test_map_from_dict_empty_returns_default(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.createDataFrame([("x",)], ["k"])
    out = df.select(f.map_from_dict({}, f.col("k"), default="fallback").alias("v")).collect()
    assert out[0]["v"] == "fallback"


def test_stable_hash_is_nonnegative_string_and_deterministic(
    spark,
    engine: PySparkEngine,  # type: ignore[no-untyped-def]
) -> None:
    f = engine.get_functions()
    df = spark.createDataFrame([("PAT1", "ENC1"), ("PAT1", "ENC1")], ["p", "e"])
    out = df.select(f.stable_hash(f.col("p"), f.col("e")).alias("id")).collect()
    ids = [r["id"] for r in out]
    # Rendered as a decimal string.
    assert all(isinstance(v, str) for v in ids)
    # Non-negative.
    assert all(int(v) >= 0 for v in ids)
    # Deterministic: same natural key → same id.
    assert ids[0] == ids[1]


def test_stable_hash_handles_nulls(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    from pyspark.sql.types import StringType, StructField, StructType

    f = engine.get_functions()
    schema = StructType([StructField("p", StringType()), StructField("e", StringType())])
    df = spark.createDataFrame([("PAT1", None)], schema)
    out = df.select(f.stable_hash(f.col("p"), f.col("e")).alias("id")).collect()
    assert int(out[0]["id"]) >= 0


def test_rand_seeded_is_reproducible(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.range(5)
    first = df.select(f.rand(seed=42).alias("r")).collect()
    second = df.select(f.rand(seed=42).alias("r")).collect()
    assert [r["r"] for r in first] == [r["r"] for r in second]


# ---- DataFrame-shape helpers on BaseEngine --------------------------------


def test_union_by_name_allow_missing_columns(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    left = spark.createDataFrame([(1, "a")], ["id", "x"])
    right = spark.createDataFrame([(2, "b")], ["id", "y"])
    out = engine.union_by_name(left, right, allow_missing_columns=True)
    rows = {r["id"]: r for r in out.collect()}
    assert set(out.columns) == {"id", "x", "y"}
    assert rows[1]["y"] is None
    assert rows[2]["x"] is None


def test_drop_duplicates_scoped(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    df = spark.createDataFrame([(1, "a"), (1, "b"), (2, "c")], ["id", "v"])
    out = engine.drop_duplicates(df, ["id"])
    assert out.count() == 2


def test_with_column(spark, engine: PySparkEngine) -> None:  # type: ignore[no-untyped-def]
    f = engine.get_functions()
    df = spark.createDataFrame([(1,)], ["n"])
    out = engine.with_column(df, "doubled", f.col("n") + f.col("n"))
    assert out.collect()[0]["doubled"] == 2
