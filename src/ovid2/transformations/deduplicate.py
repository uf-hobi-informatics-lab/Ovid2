"""Deduplicate transformer.

Delegates to the ``deduplicator`` algorithm (``composite_key`` —
:class:`ovid2.transformations.deduplicators.base.Deduplicator`),
which applies null-key passthrough and salted two-stage dedup for skew relief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ovid2.algorithms.protocol import AlgorithmKind
from ovid2.algorithms.registry import DEDUPLICATOR_NAME
from ovid2.algorithms.registry import registry as algorithms
from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class DeduplicateTransformer(BaseTransformer):
    """Composite-key deduplication with skew relief."""

    stage_name = "deduplicate"

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
        pipeline = context.get("pipeline")
        from ovid2.legacy.profile import is_legacy_ovid

        if is_legacy_ovid(pipeline):
            # Legacy: formatter W → awk dedup on the *text* (or R → W for
            # tables legacy never deduplicated). The result is the
            # ``deduplicated_<t>.csv`` text; ``LEGACY_TEXT_KEY`` tells the map
            # stage (reader side R) and the runner (raw text writer) so the
            # text is never re-written through W.
            from ovid2.legacy.csv_roundtrip import LEGACY_TEXT_KEY
            from ovid2.legacy.dedup import legacy_deduplicate

            context[LEGACY_TEXT_KEY] = True
            return legacy_deduplicate(df, str(table_name or ""), pipeline)

        if table_name is None:
            return df.dropDuplicates()
        dedup = algorithms.get(AlgorithmKind.DEDUPLICATOR, DEDUPLICATOR_NAME)
        return cast("DataFrame", dedup.apply(df, context))
