"""Portable ``pyspark.sql`` (see :mod:`ovid2.core.engines.sparkapi`)."""

from __future__ import annotations

from ovid2.core.engines.dsl import Window
from ovid2.core.engines.sparkapi._placeholders import (
    Column,
    DataFrame,
    DataFrameReader,
    DataFrameWriter,
    GroupedData,
    SparkSession,
)
from ovid2.core.engines.sparkapi.sql import column, dataframe, functions, pandas, types, window
from ovid2.core.engines.types import Row

__all__ = [
    "Column",
    "DataFrame",
    "DataFrameReader",
    "DataFrameWriter",
    "GroupedData",
    "Row",
    "SparkSession",
    "Window",
    "column",
    "dataframe",
    "functions",
    "pandas",
    "types",
    "window",
]
