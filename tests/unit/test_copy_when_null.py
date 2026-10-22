"""Unit tests for :class:`ovid2.transformations.fixers.common.copy_when_null.CopyWhenNullFixer`."""

from __future__ import annotations

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import SparkSession

from ovid2.transformations.fixers.common.copy_when_null import CopyWhenNullFixer


@pytest.mark.unit
def test_copy_when_null_fills_only_nulls(spark: SparkSession) -> None:
    """Non-null target values are preserved; null targets are replaced."""
    df = spark.createDataFrame(
        [
            ("2025-01-01", "2025-02-01"),  # target populated → keep
            (None, "2025-02-01"),  # target null      → copy source
            ("2025-01-01", None),  # source null      → keep original target
            (None, None),  # both null        → still null
        ],
        ["ONSET_DATE", "REPORT_DATE"],
    )
    context = {"src1": "ONSET_DATE", "src2": "REPORT_DATE"}
    result = CopyWhenNullFixer().apply(df, context).collect()
    filled = [row["ONSET_DATE"] for row in result]
    assert filled == ["2025-01-01", "2025-02-01", "2025-01-01", None]
