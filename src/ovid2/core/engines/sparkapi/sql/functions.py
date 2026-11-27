"""Portable ``pyspark.sql.functions``: every name dispatches to the active engine.

``from ovid2.core.engines.sparkapi.sql.functions import *`` binds every
Spark function name (:data:`ovid2.core.engines.surface.SPARK_FUNCTION_NAMES`)
plus ``udf`` / ``pandas_udf`` / ``PandasUDFType``, exactly like the PySpark
star import it replaces.
"""

from __future__ import annotations

from typing import Any

from ovid2.core.engines import dsl
from ovid2.core.engines.dsl import (  # noqa: F401 — re-exported via __all__
    PandasUDFType,
    Window,
    pandas_udf,
    udf,
)
from ovid2.core.engines.surface import SPARK_FUNCTION_NAMES

__all__ = sorted({*SPARK_FUNCTION_NAMES, "PandasUDFType", "pandas_udf", "udf"})


def __getattr__(name: str) -> Any:
    """Resolve any Spark function name through :mod:`ovid2.core.engines.dsl`."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(dsl, name)


del Window  # re-exported via ``sparkapi.sql``; imported to keep the dispatch module loaded
