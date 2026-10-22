"""Unit tests for :class:`ovid2.transformations.fixers.common.patid_orphans.PatidOrphansFixer`.

Verifies that the left-semi-join semantics remove rows whose PATID is
absent from the reference table, and that the ``src2`` override lets us
target a different PATID-like column (e.g. ``PATID_1`` for
``pat_relationship``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import SparkSession

from ovid2.transformations.fixers.common.patid_orphans import PatidOrphansFixer


def _register_demographic(spark: SparkSession, patids: list[str]) -> None:
    """Register a temp view named ``demographic`` with the given PATIDs."""
    df = spark.createDataFrame([(p,) for p in patids], ["PATID"])
    df.createOrReplaceTempView("demographic")


@pytest.mark.unit
def test_patid_orphans_removes_missing(spark: SparkSession) -> None:
    """Rows whose PATID is not in the reference table are dropped."""
    _register_demographic(spark, ["P1", "P2", "P3"])
    encounters = spark.createDataFrame(
        [
            ("E1", "P1"),
            ("E2", "P2"),
            ("E3", "P99"),  # orphan
            ("E4", "P3"),
            ("E5", "P42"),  # orphan
        ],
        ["ENCOUNTERID", "PATID"],
    )
    context = {"spark": spark, "src1": "demographic"}
    result = PatidOrphansFixer().apply(encounters, context).collect()
    kept = sorted(row["ENCOUNTERID"] for row in result)
    assert kept == ["E1", "E2", "E4"]


@pytest.mark.unit
def test_patid_orphans_custom_column_via_src2(spark: SparkSession) -> None:
    """``src2`` selects an alternate PATID-like column (e.g. PATID_1)."""
    _register_demographic(spark, ["P1", "P2"])
    rel = spark.createDataFrame(
        [
            ("P1", "P2"),
            ("P1", "P99"),
            ("P99", "P2"),  # PATID_1 is orphan
            ("P2", "P1"),
        ],
        ["PATID_1", "PATID_2"],
    )
    context = {"spark": spark, "src1": "demographic", "src2": "PATID_1"}
    result = PatidOrphansFixer().apply(rel, context).collect()
    got = sorted((r["PATID_1"], r["PATID_2"]) for r in result)
    assert got == [("P1", "P2"), ("P1", "P99"), ("P2", "P1")]


@pytest.mark.unit
def test_patid_orphans_sentinel_src2_falls_back(spark: SparkSession) -> None:
    """``src2`` sentinels (``NotUsed`` etc.) fall back to the default column."""
    _register_demographic(spark, ["P1"])
    df = spark.createDataFrame([("P1",), ("P2",)], ["PATID"])
    context = {"spark": spark, "src1": "demographic", "src2": "NotUsed"}
    result = PatidOrphansFixer().apply(df, context).collect()
    assert [row["PATID"] for row in result] == ["P1"]
