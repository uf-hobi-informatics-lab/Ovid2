"""Format transformer.

Formatting is handled by per-pipeline :class:`~ovid2.formatters.base.BaseFormatter`
classes (or the YAML spec engine, P4), invoked directly by the pipeline
before the transformer loop. This transformer is a pass-through placeholder
so ``format`` remains a nameable stage in the canonical chain.

The pipeline runner explicitly skips instances of this class in its loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class FormatTransformer(BaseTransformer):
    """Pass-through placeholder for the ``format`` stage."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Return ``df`` unchanged. See module docstring."""
        return df
