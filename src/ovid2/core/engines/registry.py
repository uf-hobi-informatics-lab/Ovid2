"""Compute-engine class registry.

Runtime-side counterpart to :mod:`ovid2.config.engines` (which owns the
web-safe form-field catalogue). Only :func:`ovid2.core.loaders.load_engine`
imports from this module.

The dotted-path strings in :data:`ENGINE_TYPE_MAP` are resolved lazily
through :func:`importlib.import_module`, so the web tier can enumerate
type names via :func:`ovid2.config.engines.list_engine_types` **without**
importing pyspark or snowpark.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ovid2.core.engines.base import BaseEngine


#: Dotted-path map. Keys are the on-disk / CLI ``--engine`` type names.
ENGINE_TYPE_MAP: dict[str, str] = {
    "pyspark": "ovid2.core.engines.pyspark.PySparkEngine",
    "databricks_spark": "ovid2.core.engines.databricks_spark.DatabricksSparkEngine",
    "snowpark": "ovid2.core.engines.snowpark.SnowparkEngine",
}


def list_engine_types() -> list[str]:
    """Return the sorted list of registered engine type names."""
    return sorted(ENGINE_TYPE_MAP)


def load_engine_class(type_name: str) -> "type[BaseEngine]":
    """Import and return the engine class registered under ``type_name``.

    Args:
        type_name: One of the keys in :data:`ENGINE_TYPE_MAP`.

    Raises:
        KeyError: If ``type_name`` is not registered.
    """
    if type_name not in ENGINE_TYPE_MAP:
        raise KeyError(
            f"Unknown engine type '{type_name}'. Known: {list_engine_types()}"
        )
    module_path, class_name = ENGINE_TYPE_MAP[type_name].rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


__all__ = ["ENGINE_TYPE_MAP", "list_engine_types", "load_engine_class"]
