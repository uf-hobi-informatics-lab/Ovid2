"""Mapper spec engine.

Walks the ordered ops list on a :class:`~ovid2.mappers.spec.MapperSpec`
and applies each op to the incoming DataFrame. Every op is stateless
and lazy — only the final :meth:`BaseAddress.write` triggers execution.

Loads per-pipeline secrets once per :func:`run` call from
``$XDG_CONFIG_HOME/ovid2/secrets.d/<pipeline>.json`` when the file
exists. Secrets never appear in a spec.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import MapOpContext
from ovid2.mappers.registry import MAP_OPS
from ovid2.mappers.spec import MapperSpec, MapperSpecValidationError

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.cdm.handle import CdmHandle

logger = logging.getLogger(__name__)


def _secrets_path(pipeline: str) -> Path:
    """Return the per-pipeline secrets file path under XDG_CONFIG_HOME."""
    import os

    root = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(root) / "ovid2" / "secrets.d" / f"{pipeline}.json"


def load_secrets(pipeline: str) -> dict[str, Any]:
    """Return the pipeline's secrets dict, or ``{}`` when unavailable.

    The file is expected to be JSON, mode 0600, at
    ``$XDG_CONFIG_HOME/ovid2/secrets.d/<pipeline>.json``.
    """
    path = _secrets_path(pipeline)
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read secrets for pipeline '%s' at %s: %s", pipeline, path, exc)
        return {}
    if not isinstance(payload, dict):
        logger.warning("Secrets for pipeline '%s' at %s is not a JSON object; ignoring", pipeline, path)
        return {}
    return payload


def _load_pipeline_dictionaries(pipeline: str) -> Any:
    """Return the pipeline's ``dictionaries`` module, or ``None``."""
    try:
        return importlib.import_module(f"ovid2_pipelines.{pipeline}.dictionaries")
    except ModuleNotFoundError:
        return None


def _resolve_class(class_path: str) -> Any:
    """Import a dotted class path and return the class object."""
    module_name, _, attr = class_path.rpartition(".")
    if not module_name or not attr:
        raise MapperSpecValidationError(
            f"Mapper class path '{class_path}' must be a fully qualified dotted name."
        )
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def run(
    df: "DataFrame",
    spec: MapperSpec,
    *,
    spark: "SparkSession",
    cdm: "CdmHandle",
    pipeline_dictionaries: Any = None,
    secrets: dict[str, Any] | None = None,
) -> "DataFrame":
    """Execute a mapper spec against ``df``.

    Args:
        df: The DataFrame to map.
        spec: The mapper spec to apply.
        spark: Active Spark session.
        cdm: The CDM handle for the pipeline.
        pipeline_dictionaries: The pipeline's ``dictionaries`` module.
            When ``None``, the engine imports it on demand.
        secrets: Pre-loaded secret bag. When ``None``, the engine loads
            from XDG_CONFIG_HOME.

    Returns:
        The mapped DataFrame (still lazy).
    """
    if not spec.enabled:
        return df

    # Escape hatch — hand off to a legacy class.
    if spec.class_path:
        klass = _resolve_class(spec.class_path)
        instance = klass()
        if pipeline_dictionaries is None:
            pipeline_dictionaries = _load_pipeline_dictionaries(spec.pipeline)
        return instance.map(df, pipeline_dictionaries, spec.pipeline)

    if pipeline_dictionaries is None:
        pipeline_dictionaries = _load_pipeline_dictionaries(spec.pipeline)

    if secrets is None:
        secrets = load_secrets(spec.pipeline)

    ctx = MapOpContext(
        spark=spark,
        pipeline=spec.pipeline,
        table=spec.table,
        cdm=cdm,
        pipeline_dictionaries=pipeline_dictionaries,
        secrets=secrets,
    )

    for op_spec in spec.ops:
        op_class = MAP_OPS.get(op_spec.op)
        if op_class is None:
            raise MapperSpecValidationError(
                f"Unknown map op '{op_spec.op}' in pipeline '{spec.pipeline}' table '{spec.table}'."
            )
        df = op_class().apply(df, op_spec.params, ctx)

    return df
