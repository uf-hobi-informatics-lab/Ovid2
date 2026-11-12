"""Distinct-values analysis transformer.

For each column that is neither an ID nor a date, group by the value,
count occurrences, log the number of distinct values and append the
top-N frequencies to the run's analysis artifact
(:mod:`ovid2.reports.artifacts`, consumed by Report Studio). Returns the
DataFrame unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.dsl import col, count, lit
from ovid2.core.engines.dsl import round as spark_round
from ovid2.core.events import log_event
from ovid2.reports.artifacts import append_analysis_record
from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)

#: Most frequent values persisted per column (Report Studio aggregate).
_TOP_VALUES = 25


class DistinctValuesTransformer(BaseTransformer):
    """Log per-column distinct-value counts for auditing."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Analyse each non-ID / non-date column; return ``df`` unchanged."""
        table_name = context.get("table_name")
        if table_name is None:
            return df

        bus = context.get("event_bus")
        instance_id = context.get("instance_id") or ""
        pipeline = context.get("pipeline") or ""

        analysis_columns = [
            c for c in df.columns if "DATE" not in c.upper() and not c.upper().endswith("ID")
        ]
        total = df.count()  # noqa: OVID2001 analysis pass — deliberate action
        if total == 0:
            return df

        for col_name in analysis_columns:
            distinct = (
                df.groupBy(col(col_name))
                .agg(count("*").alias("count"))
                .withColumn(
                    "percentage",
                    spark_round(col("count") / lit(total) * 100, 2),
                )
                .orderBy(col("count").desc())
            )
            n_distinct = distinct.count()  # noqa: OVID2001
            top = distinct.limit(_TOP_VALUES).collect()  # noqa: OVID2001 bounded top-N
            null_rows = sum(int(r["count"]) for r in top if r[col_name] is None)
            append_analysis_record(
                instance_id,
                {
                    "type": "distinct",
                    "table": table_name,
                    "column": col_name,
                    "stage": context.get("analysis_stage") or "formatted",
                    "total_rows": int(total),
                    "n_distinct": int(n_distinct),
                    "null_rows": null_rows,
                    "top": [
                        {
                            "value": None if r[col_name] is None else str(r[col_name])[:200],
                            "records": int(r["count"]),
                            "percentage": float(r["percentage"] or 0.0),
                        }
                        for r in top
                    ],
                },
            )
            message = f"distinct_values {table_name}.{col_name}: {n_distinct} unique"
            logger.info(message)
            if bus is not None:
                bus.emit(
                    log_event(
                        instance_id=instance_id,
                        level="info",
                        message=message,
                        pipeline=pipeline,
                        table=table_name,
                    )
                )
        return df


class RawDistinctValuesTransformer(BaseTransformer):
    """Distinct values on the *pre-format* source.

    Runs before formatting so it sees raw source columns (still string,
    still pipeline-specific naming). Structurally identical to
    :class:`DistinctValuesTransformer`; retained as a separate class so
    the CLI can distinguish the two stages.
    """

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Analyse the raw source; records are tagged ``stage: raw``."""
        return DistinctValuesTransformer().transform(df, {**context, "analysis_stage": "raw"})
