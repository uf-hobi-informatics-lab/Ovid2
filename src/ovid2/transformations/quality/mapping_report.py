"""Mapping report — export the pipeline's mapping dictionary as a DataFrame.

Serialises ``<table>_<column>_dict`` mappings into a wide DataFrame with
columns ``TABLE``, ``FIELD``, ``SOURCE``, ``MAPPED``, one row per entry.

The report is written to a Spark temp view named ``mapping_report`` on
the current session; the CLI can then write it via any address.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)


class MappingReportTransformer(BaseTransformer):
    """Materialise the pipeline's mapping dictionary as a Spark DataFrame."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        table_name = context.get("table_name")
        pipeline = context.get("pipeline")
        spark = context.get("spark")
        if table_name is None or pipeline is None or spark is None:
            return df

        try:
            dictionaries = importlib.import_module(f"ovid2_pipelines.{pipeline}.dictionaries")
        except ModuleNotFoundError:
            logger.warning("No dictionaries module for pipeline '%s'", pipeline)
            return df

        prefix = f"{table_name}_"
        suffix = "_dict"
        rows: list[tuple[str, str, str, str]] = []

        for dict_name in dir(dictionaries):
            if not (dict_name.startswith(prefix) and dict_name.endswith(suffix)):
                continue
            field = dict_name[len(prefix) : -len(suffix)].upper()
            pipeline_dict: dict[str, str] = getattr(dictionaries, dict_name)
            rows.extend(
                (table_name, field, str(source), str(mapped))
                for source, mapped in pipeline_dict.items()
            )

        if not rows:
            logger.info("mapping_report %s: no dictionaries found", table_name)
            return df

        schema = "TABLE_NAME string, FIELD string, SOURCE string, MAPPED string"
        report = spark.createDataFrame(rows, schema=schema)
        report.createOrReplaceTempView("mapping_report")
        logger.info("mapping_report %s: %d entries registered as view", table_name, len(rows))
        return df
