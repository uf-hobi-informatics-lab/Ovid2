"""Map transformer — thin wrapper over :mod:`ovid2.mappers.engine`.

Resolution order for one ``(pipeline, table)`` pair:

1. Declarative spec at ``pipelines/ovid2_pipelines/<pipeline>/mappers/<table>.yaml``
   (or the Python dict form). Handed to :func:`ovid2.mappers.engine.run`.
2. Legacy hand-written class at
   ``ovid2.transformations.mappers.<family>.<table>_mapper.<Table>Mapper``
   where ``<family>`` is the deprecated ``pipeline_settings.mappers_type``.
   This path is preserved so any pipeline that predates P10's declarative
   engine keeps working.
3. If neither exists, the map stage is a no-op and logs a warning.

The transformer contains no PCORnet-specific knowledge — it reads the
CDM handle from the pipeline context and passes it into the engine.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any, cast

from ovid2.mappers.engine import run as run_spec
from ovid2.mappers.spec import MapperSpecNotFoundError, load_spec
from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)

# Deprecated. Retained for the legacy class-path fallback only.
# ``pipeline_settings.mappers_type`` → mapper-package dotted path.
_LEGACY_MAPPER_TYPE_MODULES: dict[str, str] = {
    "pcornet_mappers": "ovid2.transformations.mappers.pcornet",
    "veritas_mappers": "ovid2.transformations.mappers.veritas",
    "tumor_mappers": "ovid2.transformations.mappers.tumor",
    "nursing_mappers": "ovid2.transformations.mappers.nursing",
}


class MapTransformer(BaseTransformer):
    """Apply the declarative mapper spec (or a legacy mapper class)."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Apply the mapper for ``context['table_name']``.

        Args:
            df: Input DataFrame.
            context: Must contain ``table_name``, ``pipeline``, ``spark``,
                and ``cdm``.

        Returns:
            The mapped DataFrame (still lazy).
        """
        table_name = context.get("table_name")
        pipeline = context.get("pipeline")
        spark = context.get("spark")
        cdm = context.get("cdm")

        if table_name is None or pipeline is None or spark is None or cdm is None:
            return df

        # 1) Declarative spec.
        try:
            spec = load_spec(pipeline, table_name)
        except MapperSpecNotFoundError:
            spec = None

        if spec is not None:
            return run_spec(df, spec, spark=spark, cdm=cdm)

        # 2) Legacy class path (deprecated).
        legacy = self._load_legacy_mapper(table_name, pipeline)
        if legacy is not None:
            mapper, dictionaries = legacy
            return cast("DataFrame", mapper.map(df, dictionaries, pipeline))

        # 3) No mapper — leave the DataFrame untouched.
        logger.info(
            "map: no spec or legacy mapper for pipeline '%s' table '%s'; passing through",
            pipeline,
            table_name,
        )
        return df

    @staticmethod
    def _load_legacy_mapper(table_name: str, pipeline: str) -> tuple[Any, Any] | None:
        """Return ``(mapper_instance, dictionaries_module)`` if a legacy class exists."""
        try:
            settings = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
        except ModuleNotFoundError:
            return None

        mappers_type = str(getattr(settings, "mappers_type", "pcornet_mappers"))
        base_module = _LEGACY_MAPPER_TYPE_MODULES.get(mappers_type)
        if base_module is None:
            return None

        module_name = f"{base_module}.{table_name}_mapper"
        class_name = "".join(part.capitalize() for part in table_name.split("_")) + "Mapper"

        try:
            module = importlib.import_module(module_name)
            mapper_class = getattr(module, class_name)
        except (ModuleNotFoundError, AttributeError):
            return None

        try:
            dictionaries = importlib.import_module(f"ovid2_pipelines.{pipeline}.dictionaries")
        except ModuleNotFoundError:
            dictionaries = None

        return mapper_class(), dictionaries
