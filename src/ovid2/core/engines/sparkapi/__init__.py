"""Engine-portable stand-in for the ``pyspark`` package (legacy-parity code).

Generated legacy ports (``formatters_custom``) import this package instead
of PySpark (:mod:`ovid2.tools.legacy_formatter_migrator` rewrites
``import pyspark`` / ``from pyspark.sql.functions import *`` …), and the
vendored legacy fix scripts see it through :func:`overlay` on non-Spark
engines. Every name resolves to the engine-portable layer:

* ``sparkapi.sql.functions`` → :mod:`ovid2.core.engines.dsl` (each call
  dispatches to the active engine: real PySpark on Spark, DuckDB SQL on
  DuckDB, Snowpark on Snowpark);
* ``sparkapi.sql.types`` → :mod:`ovid2.core.engines.types` (DDL-string
  types PySpark accepts unchanged);
* ``sparkapi.sql.Window`` → the dispatching ``dsl.Window``.

``SparkSession`` / ``SparkConf`` / ``SparkContext`` / ``DataFrame`` /
``Column`` are inert names: legacy code only annotates with them, and its
sessions come from the shim's ``get_spark_session``.

Never imports pyspark.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator

from ovid2.core.engines.sparkapi import sql
from ovid2.core.engines.sparkapi._placeholders import SparkConf, SparkContext, StorageLevel

__all__ = ["SparkConf", "SparkContext", "StorageLevel", "overlay", "sql"]

#: ``pyspark`` module name -> portable module (see :func:`overlay`).
_ALIASES: tuple[tuple[str, str], ...] = (
    ("pyspark", "ovid2.core.engines.sparkapi"),
    ("pyspark.sql", "ovid2.core.engines.sparkapi.sql"),
    ("pyspark.sql.functions", "ovid2.core.engines.sparkapi.sql.functions"),
    ("pyspark.sql.types", "ovid2.core.engines.sparkapi.sql.types"),
    ("pyspark.sql.window", "ovid2.core.engines.sparkapi.sql.window"),
    ("pyspark.sql.column", "ovid2.core.engines.sparkapi.sql.column"),
    ("pyspark.sql.dataframe", "ovid2.core.engines.sparkapi.sql.dataframe"),
    ("pyspark.sql.pandas", "ovid2.core.engines.sparkapi.sql.pandas"),
    ("pyspark.sql.pandas.functions", "ovid2.core.engines.sparkapi.sql.pandas.functions"),
)


@contextlib.contextmanager
def overlay() -> Iterator[None]:
    """Bind ``pyspark.*`` imports to this package for the enclosed block.

    Used only around verbatim vendored legacy code running on a non-Spark
    engine; the previous ``sys.modules`` entries are restored on exit.
    """
    import importlib

    saved = {name: sys.modules.get(name) for name, _ in _ALIASES}
    try:
        for name, target in _ALIASES:
            sys.modules[name] = importlib.import_module(target)
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library package.")
