"""Monthly-counts analysis transformer.

Groups records by ``yyyy-MM`` for every column containing ``DATE`` in
its name. Logs the number of distinct months found for each column;
returns the input DataFrame unchanged (pass-through) so the pipeline
can continue.

Emits ``log`` events on the event bus at INFO level for each analysed
column.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyspark.sql.functions import col, count, date_format, to_date

from ovid2.core.events import log_event
from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)


class MonthlyCountsTransformer(BaseTransformer):
    """Compute monthly record counts for every ``*DATE*`` column."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Log a distinct-months summary per date column; return ``df`` unchanged."""
        table_name = context.get("table_name")
        if table_name is None:
            return df

        bus = context.get("event_bus")
        instance_id = context.get("instance_id") or ""
        pipeline = context.get("pipeline") or ""

        date_columns = [c for c in df.columns if "DATE" in c.upper()]
        for date_col in date_columns:
            monthly = (
                df.withColumn("_year_month", date_format(to_date(col(date_col)), "yyyy-MM"))
                .filter(col("_year_month").isNotNull())
                .groupBy("_year_month")
                .agg(count("*").alias("record_count"))
                .orderBy(col("_year_month").desc())
            )
            n_months = monthly.count()  # noqa: OVID2001 analysis pass — deliberate action
            message = f"monthly_counts {table_name}.{date_col}: {n_months} distinct months"
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
