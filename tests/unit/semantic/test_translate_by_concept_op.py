"""Unit tests for the ``translate_by_concept`` mapper op (S2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyspark")

from ovid2.cdm.registry import get_cdm
from ovid2.mappers.base import MapOpContext
from ovid2.mappers.ops.translate_by_concept import TranslateByConceptOp

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


def _ctx(spark: "SparkSession", table: str = "diagnosis") -> MapOpContext:
    """Build a minimal :class:`MapOpContext` for tests."""
    return MapOpContext(
        spark=spark,
        pipeline="testpipeline",
        table=table,
        cdm=get_cdm("pcornet_7_0"),
        pipeline_dictionaries=None,
        secrets={},
    )


@pytest.mark.unit
def test_translate_by_concept_basic_lookup(spark: "SparkSession") -> None:
    """A matched value is replaced with the vocabulary's target column."""
    concept = spark.createDataFrame(
        [
            ("I10", "SNOMED", "Hypertension"),
            ("E11", "SNOMED", "Diabetes"),
        ],
        ["concept_code", "vocabulary_id", "concept_name"],
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10"), ("P2", "E11")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {"column": "DX", "vocabulary": "omop_concept", "target": "concept_name"},
        _ctx(spark),
    )
    rows = sorted((r["PATID"], r["DX"]) for r in result.collect())
    assert rows == [("P1", "Hypertension"), ("P2", "Diabetes")]


@pytest.mark.unit
def test_translate_by_concept_keeps_unmatched(spark: "SparkSession") -> None:
    """Unmatched values pass through unchanged by default."""
    concept = spark.createDataFrame(
        [("I10", "Hypertension")], ["concept_code", "concept_name"]
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10"), ("P2", "UNKNOWN")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {"column": "DX", "vocabulary": "omop_concept", "target": "concept_name"},
        _ctx(spark),
    )
    rows = sorted((r["PATID"], r["DX"]) for r in result.collect())
    assert rows == [("P1", "Hypertension"), ("P2", "UNKNOWN")]


@pytest.mark.unit
def test_translate_by_concept_drops_unmatched_when_flag_set(spark: "SparkSession") -> None:
    """``keep_unmatched=False`` drops rows with no vocabulary match."""
    concept = spark.createDataFrame(
        [("I10", "Hypertension")], ["concept_code", "concept_name"]
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10"), ("P2", "UNKNOWN")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {
            "column": "DX",
            "vocabulary": "omop_concept",
            "target": "concept_name",
            "keep_unmatched": False,
        },
        _ctx(spark),
    )
    rows = sorted((r["PATID"], r["DX"]) for r in result.collect())
    assert rows == [("P1", "Hypertension")]


@pytest.mark.unit
def test_translate_by_concept_case_insensitive(spark: "SparkSession") -> None:
    """Case-insensitive matching is the default."""
    concept = spark.createDataFrame(
        [("i10", "Hypertension")], ["concept_code", "concept_name"]
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {"column": "DX", "vocabulary": "omop_concept", "target": "concept_name"},
        _ctx(spark),
    )
    rows = [(r["PATID"], r["DX"]) for r in result.collect()]
    assert rows == [("P1", "Hypertension")]


@pytest.mark.unit
def test_translate_by_concept_case_sensitive_flag(spark: "SparkSession") -> None:
    """``case_sensitive=True`` rejects case-mismatched values."""
    concept = spark.createDataFrame(
        [("i10", "Hypertension")], ["concept_code", "concept_name"]
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {
            "column": "DX",
            "vocabulary": "omop_concept",
            "target": "concept_name",
            "case_sensitive": True,
        },
        _ctx(spark),
    )
    rows = [(r["PATID"], r["DX"]) for r in result.collect()]
    # Case mismatch → no vocabulary match → passthrough of "I10".
    assert rows == [("P1", "I10")]


@pytest.mark.unit
def test_translate_by_concept_target_column_override(spark: "SparkSession") -> None:
    """Write the mapped value into a different column."""
    concept = spark.createDataFrame(
        [("I10", "Hypertension")], ["concept_code", "concept_name"]
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {
            "column": "DX",
            "vocabulary": "omop_concept",
            "target": "concept_name",
            "target_column": "DX_LABEL",
        },
        _ctx(spark),
    )
    row = result.collect()[0].asDict()
    assert row["PATID"] == "P1"
    assert row["DX"] == "I10"
    assert row["DX_LABEL"] == "Hypertension"


@pytest.mark.unit
def test_translate_by_concept_domain_filter(spark: "SparkSession") -> None:
    """``domain_filter`` restricts the vocabulary side via ``domain_column``."""
    concept = spark.createDataFrame(
        [
            ("I10", "Hypertension", "Condition"),
            ("I10", "Some drug",   "Drug"),
        ],
        ["concept_code", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])

    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {
            "column": "DX",
            "vocabulary": "omop_concept",
            "target": "concept_name",
            "domain_filter": ["Condition"],
        },
        _ctx(spark),
    )
    rows = [(r["PATID"], r["DX"]) for r in result.collect()]
    assert rows == [("P1", "Hypertension")]


@pytest.mark.unit
def test_translate_by_concept_unknown_vocabulary_is_noop(spark: "SparkSession") -> None:
    """An unregistered vocabulary logs and passes the DataFrame through."""
    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])
    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {"column": "DX", "vocabulary": "nonexistent"},
        _ctx(spark),
    )
    rows = [(r["PATID"], r["DX"]) for r in result.collect()]
    assert rows == [("P1", "I10")]


@pytest.mark.unit
def test_translate_by_concept_missing_view_is_noop(spark: "SparkSession") -> None:
    """When the vocabulary view is not in Spark's catalog, the op no-ops.

    Simulated by dropping the ``concept`` temp view before the op runs.
    """
    # Ensure the view does not exist.
    spark.catalog.dropTempView("concept")

    df = spark.createDataFrame([("P1", "I10")], ["PATID", "DX"])
    op = TranslateByConceptOp()
    result = op.apply(
        df,
        {"column": "DX", "vocabulary": "omop_concept"},
        _ctx(spark),
    )
    rows = [(r["PATID"], r["DX"]) for r in result.collect()]
    assert rows == [("P1", "I10")]


@pytest.mark.unit
def test_translate_by_concept_op_name() -> None:
    """The op identifier matches the registry key."""
    assert TranslateByConceptOp().name == "translate_by_concept"


@pytest.mark.unit
def test_translate_by_concept_registered_in_map_ops() -> None:
    """The op is wired into the mapper registry."""
    from ovid2.mappers.registry import MAP_OPS

    assert "translate_by_concept" in MAP_OPS
    assert MAP_OPS["translate_by_concept"] is TranslateByConceptOp
