"""Mapping-gap analysis transformer.

Identifies values in a table's formatted columns that are not present in
the pipeline's mapping dictionaries. For every dictionary named
``<table>_<column>_dict`` under ``ovid2_pipelines.<pipeline>.dictionaries``,
counts unmapped distinct values and emits a log event.

Returns the DataFrame unchanged.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from pyspark.sql.functions import col, count, lit, upper, when
from pyspark.sql.functions import round as spark_round

from ovid2.core.events import log_event
from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)

FLAVOR_OF_NULL: frozenset[str | None] = frozenset({"NI", "OT", "UN", "", None})


class MappingGapTransformer(BaseTransformer):
    """Report dictionary-coverage gaps for a table."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Analyse each dictionary-mapped column; return ``df`` unchanged."""
        table_name = context.get("table_name")
        pipeline = context.get("pipeline")
        if table_name is None or pipeline is None:
            return df

        try:
            dictionaries = importlib.import_module(f"ovid2_pipelines.{pipeline}.dictionaries")
        except ModuleNotFoundError:
            logger.warning("No dictionaries module for pipeline '%s'", pipeline)
            return df

        bus = context.get("event_bus")
        instance_id = context.get("instance_id") or ""

        prefix = f"{table_name}_"
        suffix = "_dict"
        dict_names = [
            name for name in dir(dictionaries) if name.startswith(prefix) and name.endswith(suffix)
        ]

        total = df.count()  # noqa: OVID2001 analysis pass — deliberate action
        if total == 0:
            return df

        for dict_name in dict_names:
            field_name = dict_name[len(prefix) : -len(suffix)].upper()
            if field_name not in df.columns:
                continue

            pipeline_dict: dict[str, str] = getattr(dictionaries, dict_name)
            mapped_upper = {k.upper() if isinstance(k, str) else k for k in pipeline_dict}
            fon_entries = {k: v for k, v in pipeline_dict.items() if v in FLAVOR_OF_NULL}

            value_counts = (
                df.groupBy(col(field_name))
                .agg(count("*").alias("count"))
                .withColumn(
                    "percentage",
                    spark_round(col("count") / lit(total) * 100, 2),
                )
                .orderBy(col("count").desc())
            )
            gap_df = value_counts.withColumn(
                "is_mapped",
                when(
                    upper(col(field_name)).isin(list(mapped_upper)),
                    lit("Y"),
                ).otherwise(lit("N")),
            )
            unmapped = gap_df.filter(col("is_mapped") == "N").count()  # noqa: OVID2001

            if unmapped > 0:
                message = (
                    f"mapping_gap {table_name}.{field_name}: {unmapped} unmapped distinct values"
                )
                logger.info(message)
                if bus is not None:
                    bus.emit(
                        log_event(
                            instance_id=instance_id,
                            level="info",
                            message=message,
                            pipeline=str(pipeline),
                            table=table_name,
                        )
                    )

            if fon_entries:
                fon_message = (
                    f"flavor_of_null {table_name}.{field_name}: "
                    f"{len(fon_entries)} dict entries → NI/OT/UN"
                )
                logger.info(fon_message)
                if bus is not None:
                    bus.emit(
                        log_event(
                            instance_id=instance_id,
                            level="info",
                            message=fon_message,
                            pipeline=str(pipeline),
                            table=table_name,
                        )
                    )

        return df
