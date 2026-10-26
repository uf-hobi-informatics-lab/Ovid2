"""``trim`` — strip surrounding whitespace from columns.

Params:
    columns: List of column names to trim.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


class TrimTransform(BaseTransform):
    """Strip leading and trailing whitespace from one or more columns."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "trim"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Strip leading and trailing whitespace from one or more columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to trim (rewritten in place).",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns trimmed."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("trim: 'columns' must be a non-empty list.")
        f = ctx.functions
        for column in columns:
            name = str(column)
            df = df.withColumn(name, f.trim(f.col(name)))
        return df


class NormalizeWhitespaceTransform(BaseTransform):
    """Collapse internal whitespace runs to a single space, then trim.

    Turns ``"  Mr   John  "`` into ``"Mr John"``.
    """

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "normalize_whitespace"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Collapse repeated whitespace to single spaces and trim the ends."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to normalise (rewritten in place).",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with whitespace normalised in the named columns."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("normalize_whitespace: 'columns' must be a non-empty list.")
        f = ctx.functions
        for column in columns:
            name = str(column)
            collapsed = f.regexp_replace(f.col(name), r"\s+", " ")
            df = df.withColumn(name, f.trim(collapsed))
        return df


__all__ = ["NormalizeWhitespaceTransform", "TrimTransform"]
