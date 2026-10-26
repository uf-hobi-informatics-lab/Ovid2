"""Family-gating helper.

Answers the question "can this pipeline run under this engine?" by
consulting :class:`~ovid2.core.engines.protocols.EngineFamily` on the
engine and the ``requires_family`` marker on pipeline_settings.

A pipeline marks itself Spark-only by declaring::

    # pipelines/ovid2_pipelines/<name>/pipeline_settings.py
    REQUIRES_FAMILY: str = "spark"

The default (no marker) is "spark" because every existing pipeline
uses PySpark today. Once a pipeline is verified to run under Snowpark
the marker can be dropped or set to ``None`` (portable).

This module is deliberately tiny — most of the checking happens at
the CLI ``ovid2 etl`` entry point where we already have both the
engine and the pipeline name in hand.
"""

from __future__ import annotations

import importlib
import logging

from ovid2.core.engines.base import BaseEngine
from ovid2.core.engines.protocols import EngineFamily

logger = logging.getLogger(__name__)


def resolve_pipeline_family(pipeline: str) -> str | None:
    """Return the pipeline's declared ``REQUIRES_FAMILY``, or ``None``.

    ``None`` means the pipeline is portable across engine families.
    Missing pipeline_settings modules also return ``None`` — the caller
    will hit the real error later from the loader.
    """
    try:
        module = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        return None
    value = getattr(module, "REQUIRES_FAMILY", None)
    if value is None:
        return None
    return str(value).lower()


def assert_pipeline_engine_compatible(engine: BaseEngine, pipeline: str) -> None:
    """Raise :class:`RuntimeError` when ``pipeline`` cannot run under ``engine``.

    Args:
        engine: The compute engine returned by
            :func:`ovid2.core.loaders.load_engine`.
        pipeline: Pipeline name (e.g. ``'demo'``).

    Raises:
        RuntimeError: When the pipeline requires a different family.
    """
    required = resolve_pipeline_family(pipeline)
    if required is None:
        return
    engine_family = (
        engine.family.value if isinstance(engine.family, EngineFamily) else str(engine.family)
    )
    if required != engine_family:
        raise RuntimeError(
            f"Pipeline '{pipeline}' requires engine family '{required}' "
            f"(REQUIRES_FAMILY in pipeline_settings), but engine "
            f"'{engine.type_name}' is family '{engine_family}'. "
            f"Pick a compatible engine or update the pipeline's marker."
        )


__all__ = ["assert_pipeline_engine_compatible", "resolve_pipeline_family"]
