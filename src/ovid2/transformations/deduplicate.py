"""Deduplicate transformer.

Delegates to :class:`ovid2.transformations.deduplicators.base.Deduplicator`,
which applies null-key passthrough and salted two-stage dedup for skew relief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.transformations.base import BaseTransformer
from ovid2.transformations.deduplicators.base import Deduplicator

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class DeduplicateTransformer(BaseTransformer):
    """Composite-key deduplication with skew relief."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove duplicates based on the table's composite key.

        Args:
            df: Input Spark DataFrame.
            context: Pipeline context. Reads ``table_name`` and, when
                present, the ``cdm`` handle. When ``cdm`` is missing the
                :class:`Deduplicator` falls back to the pcornet default.

        Returns:
            Deduplicated DataFrame (lazy).
        """
        table_name = context.get("table_name")
        if table_name is None:
            return df.dropDuplicates()
        cdm = context.get("cdm")
        engine = context.get("engine")
        return Deduplicator(table_name, cdm=cdm, engine=engine).deduplicate(df)
