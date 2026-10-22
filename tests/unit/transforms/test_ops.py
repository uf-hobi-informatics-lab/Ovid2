"""Unit tests for the public transformations library.

Exercises a representative slice of the ships-with op vocabulary
(formatting, correction, reshape, sampling), the spec loader, and the
engine's family gating. Uses the shared ``spark`` fixture and the
Spark-family portable functions adapter, mirroring the real pipeline
call path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import SparkSession

from ovid2.core.engines.pyspark import spark_functions
from ovid2.transforms import TransformFamilyError, load_from_dict, run
from ovid2.transforms.base import TransformContext


def _ctx(spark: SparkSession, family: str = "spark") -> TransformContext:
    """Build a Spark-backed transform context for a test."""
    return TransformContext(
        session=spark,
        functions=spark_functions(),
        engine_family=family,
    )


def _run(spark: SparkSession, df, steps: list[dict], family: str = "spark"):
    """Run an inline transform chain against ``df``."""
    spec = load_from_dict({"v": 1, "steps": steps}, name="test")
    return run(
        df,
        spec,
        session=spark,
        functions=spark_functions(),
        engine_family=family,
    )


@pytest.mark.unit
def test_formatting_chain(spark: SparkSession) -> None:
    """trim + to_upper + normalize_whitespace behave as expected."""
    df = spark.createDataFrame([(" m ", "  a  b ")], ["SEX", "NAME"])
    out = _run(
        spark,
        df,
        [
            {"op": "trim", "columns": ["SEX"]},
            {"op": "to_upper", "columns": ["SEX"]},
            {"op": "normalize_whitespace", "columns": ["NAME"]},
        ],
    )
    row = out.collect()[0].asDict()
    assert row["SEX"] == "M"
    assert row["NAME"] == "a b"


@pytest.mark.unit
def test_strip_non_numeric_and_clamp(spark: SparkSession) -> None:
    """strip_non_numeric cleans text; clamp bounds the cast value."""
    df = spark.createDataFrame([("12,3",), ("999",)], ["AGE"])
    out = _run(
        spark,
        df,
        [
            {"op": "strip_non_numeric", "columns": ["AGE"]},
            {"op": "cast", "types": {"AGE": "int"}},
            {"op": "clamp", "column": "AGE", "min": 0, "max": 120},
        ],
    )
    # "12,3" -> "123" -> 123 -> clamp to 120; "999" -> 999 -> clamp to 120.
    ages = sorted(r["AGE"] for r in out.collect())
    assert ages == [120, 120]


@pytest.mark.unit
def test_clamp_bounds(spark: SparkSession) -> None:
    """clamp pins values below min and above max."""
    df = spark.createDataFrame([(-5,), (50,), (999,)], ["V"])
    out = _run(spark, df, [{"op": "clamp", "column": "V", "min": 0, "max": 120}])
    assert sorted(r["V"] for r in out.collect()) == [0, 50, 120]


@pytest.mark.unit
def test_null_if_equals(spark: SparkSession) -> None:
    """null_if nulls listed sentinel values."""
    df = spark.createDataFrame([("999",), ("5",)], ["R"])
    out = _run(spark, df, [{"op": "null_if", "column": "R", "equals": ["999"]}])
    values = sorted((r["R"] is None, r["R"]) for r in out.collect())
    assert values == [(False, "5"), (True, None)]


@pytest.mark.unit
def test_standardize_null(spark: SparkSession) -> None:
    """standardize_null converts sentinels to true null."""
    df = spark.createDataFrame([("NULL",), (" . ",), ("real",)], ["X"])
    out = _run(spark, df, [{"op": "standardize_null", "columns": ["X"]}])
    nulls = sum(1 for r in out.collect() if r["X"] is None)
    assert nulls == 2


@pytest.mark.unit
def test_dedup_subset(spark: SparkSession) -> None:
    """dedup drops duplicates on the subset key."""
    df = spark.createDataFrame([("a", 1), ("a", 2), ("b", 3)], ["K", "V"])
    out = _run(spark, df, [{"op": "dedup", "subset": ["K"]}])
    assert out.count() == 2


@pytest.mark.unit
def test_reduce_group_max(spark: SparkSession) -> None:
    """reduce groups and aggregates."""
    df = spark.createDataFrame([("p1", 10), ("p1", 20), ("p2", 5)], ["PATID", "VAL"])
    out = _run(
        spark,
        df,
        [{"op": "reduce", "group_by": ["PATID"], "aggs": {"VAL": "max"}}],
    )
    result = {r["PATID"]: r["max(VAL)"] for r in out.collect()}
    assert result == {"p1": 20, "p2": 5}


@pytest.mark.unit
def test_unpivot(spark: SparkSession) -> None:
    """unpivot melts wide value columns into long rows."""
    df = spark.createDataFrame([("p1", 120, 80)], ["PATID", "SYS", "DIA"])
    out = _run(
        spark,
        df,
        [
            {
                "op": "unpivot",
                "id_columns": ["PATID"],
                "value_columns": ["SYS", "DIA"],
                "name_to": "MEASURE",
                "value_to": "VALUE",
            }
        ],
    )
    rows = {(r["MEASURE"], r["VALUE"]) for r in out.collect()}
    assert rows == {("SYS", 120), ("DIA", 80)}


@pytest.mark.unit
def test_head_by_group_latest(spark: SparkSession) -> None:
    """head_by_group keeps the latest row per key."""
    df = spark.createDataFrame(
        [("p1", "2020-01-01", 1), ("p1", "2021-01-01", 2), ("p2", "2019-01-01", 3)],
        ["PATID", "DT", "V"],
    )
    out = _run(
        spark,
        df,
        [
            {
                "op": "head_by_group",
                "partition_by": ["PATID"],
                "order_by": [{"col": "DT", "desc": True}],
                "n": 1,
            }
        ],
    )
    result = {r["PATID"]: r["V"] for r in out.collect()}
    assert result == {"p1": 2, "p2": 3}


@pytest.mark.unit
def test_limit_is_lazy_and_bounded(spark: SparkSession) -> None:
    """limit keeps at most n rows."""
    df = spark.createDataFrame([(i,) for i in range(100)], ["N"])
    out = _run(spark, df, [{"op": "limit", "n": 10}])
    assert out.count() == 10


@pytest.mark.unit
def test_random_sample_seed_reproducible(spark: SparkSession) -> None:
    """random_sample with a seed is reproducible."""
    df = spark.createDataFrame([(i,) for i in range(1000)], ["N"])
    a = _run(spark, df, [{"op": "random_sample", "fraction": 0.1, "seed": 42}])
    b = _run(spark, df, [{"op": "random_sample", "fraction": 0.1, "seed": 42}])
    assert sorted(r["N"] for r in a.collect()) == sorted(r["N"] for r in b.collect())


@pytest.mark.unit
def test_family_gating_blocks_spark_only_op(spark: SparkSession) -> None:
    """A spark-only op is refused under a snowpark family."""
    df = spark.createDataFrame([("p1", "2020-01-01")], ["PATID", "DT"])
    with pytest.raises(TransformFamilyError):
        _run(
            spark,
            df,
            [
                {
                    "op": "add_row_number",
                    "partition_by": ["PATID"],
                    "order_by": [{"col": "DT"}],
                    "output_column": "RN",
                }
            ],
            family="snowpark",
        )


@pytest.mark.unit
def test_unknown_op_fails_fast(spark: SparkSession) -> None:
    """An unregistered op name raises before any execution."""
    from ovid2.transforms.spec import TransformSpecValidationError

    df = spark.createDataFrame([(1,)], ["N"])
    # Bypass schema (pattern-valid but unregistered name).
    spec = load_from_dict({"v": 1, "steps": [{"op": "nonexistent_op"}]}, name="bad")
    with pytest.raises(TransformSpecValidationError):
        run(df, spec, session=spark, functions=spark_functions(), engine_family="spark")


@pytest.mark.unit
def test_disabled_spec_is_noop(spark: SparkSession) -> None:
    """An `enabled: false` spec returns the input unchanged."""
    df = spark.createDataFrame([(" x ",)], ["C"])
    spec = load_from_dict(
        {"v": 1, "enabled": False, "steps": [{"op": "trim", "columns": ["C"]}]},
        name="off",
    )
    out = run(df, spec, session=spark, functions=spark_functions(), engine_family="spark")
    assert out.collect()[0]["C"] == " x "
