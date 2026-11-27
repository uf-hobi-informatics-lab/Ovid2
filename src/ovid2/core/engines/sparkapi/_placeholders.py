"""Inert PySpark class names (used only by name in legacy code)."""

from __future__ import annotations

from typing import Any

__all__ = [
    "Column",
    "DataFrame",
    "DataFrameReader",
    "DataFrameWriter",
    "GroupedData",
    "SparkConf",
    "SparkContext",
    "SparkSession",
    "StorageLevel",
    "WindowSpec",
]


class _Placeholder:
    """A name legacy code imports but only uses in annotations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class SparkSession(_Placeholder):
    """Sessions come from the legacy shim (``cf.get_spark_session``)."""

    builder: Any = None


class SparkConf(_Placeholder):
    """Placeholder for ``pyspark.SparkConf``."""


class SparkContext(_Placeholder):
    """Placeholder for ``pyspark.SparkContext``."""


class StorageLevel(_Placeholder):
    """Placeholder for ``pyspark.StorageLevel`` (levels are ignored off Spark)."""

    MEMORY_AND_DISK: Any = None
    MEMORY_ONLY: Any = None
    DISK_ONLY: Any = None


class DataFrame(_Placeholder):
    """Placeholder for ``pyspark.sql.DataFrame``."""


class Column(_Placeholder):
    """Placeholder for ``pyspark.sql.Column``."""


class GroupedData(_Placeholder):
    """Placeholder for ``pyspark.sql.GroupedData``."""


class DataFrameReader(_Placeholder):
    """Placeholder for ``pyspark.sql.DataFrameReader``."""


class DataFrameWriter(_Placeholder):
    """Placeholder for ``pyspark.sql.DataFrameWriter``."""


class WindowSpec(_Placeholder):
    """Placeholder for ``pyspark.sql.window.WindowSpec``."""
