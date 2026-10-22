"""Unit tests for :class:`ovid2.transformations.deduplicators.base.Deduplicator`.

Uses the shared ``spark`` fixture in :mod:`tests.conftest`. Auto-skipped
when no JVM is available.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import SparkSession

from ovid2.transformations.deduplicators.base import Deduplicator


@pytest.mark.unit
def test_dedup_basic(spark: SparkSession) -> None:
    """Rows with identical composite keys collapse to one."""
    df = spark.createDataFrame(
        [
            ("P1",),
            ("P1",),
            ("P2",),
            ("P3",),
            ("P3",),
        ],
        ["PATID"],
    )
    result = Deduplicator("demographic").deduplicate(df).collect()
    patids = sorted(row["PATID"] for row in result)
    assert patids == ["P1", "P2", "P3"]


@pytest.mark.unit
def test_dedup_composite_key(spark: SparkSession) -> None:
    """Only rows matching on all composite-key columns collapse."""
    df = spark.createDataFrame(
        [
            ("E1", "250.00", "09"),
            ("E1", "250.00", "09"),  # duplicate
            ("E1", "250.00", "10"),  # different DX_TYPE — not a duplicate
            ("E2", "250.00", "09"),
        ],
        ["ENCOUNTERID", "DX", "DX_TYPE"],
    )
    result = Deduplicator("diagnosis").deduplicate(df).collect()
    keys = sorted((row["ENCOUNTERID"], row["DX"], row["DX_TYPE"]) for row in result)
    assert keys == [
        ("E1", "250.00", "09"),
        ("E1", "250.00", "10"),
        ("E2", "250.00", "09"),
    ]


@pytest.mark.unit
def test_dedup_null_key_passthrough(spark: SparkSession) -> None:
    """Rows with any null key column pass through unchanged (never dedup'd)."""
    df = spark.createDataFrame(
        [
            ("P1",),
            ("P1",),
            (None,),
            (None,),  # two null-key rows must both survive
            ("",),  # empty-string key is also passthrough
            ("",),
        ],
        ["PATID"],
    )
    result = Deduplicator("demographic").deduplicate(df).collect()
    patids = sorted(str(row["PATID"]) for row in result)
    # One "P1" (deduped), two "None"s (passthrough), two ""s (passthrough)
    assert patids == ["", "", "None", "None", "P1"]


@pytest.mark.unit
def test_dedup_unknown_table_raises() -> None:
    """Instantiating for an unknown table fails with a helpful message."""
    with pytest.raises(ValueError, match="No composite key"):
        Deduplicator("not_a_real_table")
