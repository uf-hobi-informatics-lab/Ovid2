"""Compute engine plugins.

Every engine implements :class:`BaseEngine`. Ovid2 ships:

* :class:`PySparkEngine` — local / cluster Spark (default).
* :class:`DatabricksSparkEngine` — Databricks Connect.
* :class:`SnowparkEngine` — Snowflake Snowpark.

Load an engine by name through :func:`ovid2.core.loaders.load_engine`;
enumerate available types through
:func:`ovid2.core.engines.registry.list_engine_types`.
"""

from __future__ import annotations

from ovid2.core.engines.base import BaseEngine, EngineTestResult
from ovid2.core.engines.family import (
    assert_pipeline_engine_compatible,
    resolve_pipeline_family,
)
from ovid2.core.engines.metrics import EngineMetricsSample
from ovid2.core.engines.protocols import (
    EngineFamily,
    EngineFunctions,
    EngineOperationUnsupported,
    EngineSession,
)
from ovid2.core.engines.registry import (
    ENGINE_TYPE_MAP,
    list_engine_types,
    load_engine_class,
)

__all__ = [
    "ENGINE_TYPE_MAP",
    "BaseEngine",
    "EngineFamily",
    "EngineFunctions",
    "EngineMetricsSample",
    "EngineOperationUnsupported",
    "EngineSession",
    "EngineTestResult",
    "assert_pipeline_engine_compatible",
    "list_engine_types",
    "load_engine_class",
    "resolve_pipeline_family",
]
