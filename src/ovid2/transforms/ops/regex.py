"""``regex_replace`` — apply a regular-expression substitution.

Params:
    columns: List of columns to apply the substitution to.
    pattern: Regular expression to match.
    replacement: Replacement string (may reference capture groups).
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


class RegexReplaceTransform(BaseTransform):
    """Regex-substitute values across one or more columns."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "regex_replace"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Replace regex matches with a replacement string across columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["columns", "pattern", "replacement"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to apply the substitution to.",
                },
                "pattern": {"type": "string", "description": "Regex to match."},
                "replacement": {
                    "type": "string",
                    "description": "Replacement (may reference capture groups, e.g. $1).",
                },
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the regex substitution applied."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("regex_replace: 'columns' must be a non-empty list.")
        pattern = str(params["pattern"])
        replacement = str(params["replacement"])
        f = ctx.functions
        for column in columns:
            name = str(column)
            df = df.withColumn(name, f.regexp_replace(f.col(name), pattern, replacement))
        return df


class StripNonNumericTransform(BaseTransform):
    """Remove every non-digit character from the named columns.

    ``"12,345 units"`` becomes ``"12345"``.
    """

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "strip_non_numeric"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Remove all non-digit characters from one or more columns."

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
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with non-digit characters stripped."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("strip_non_numeric: 'columns' must be a non-empty list.")
        f = ctx.functions
        for column in columns:
            name = str(column)
            df = df.withColumn(name, f.regexp_replace(f.col(name), r"[^0-9]", ""))
        return df


class StripNonAlphaTransform(BaseTransform):
    """Remove every non-letter character from the named columns."""

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "strip_non_alpha"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Remove all non-letter characters from one or more columns."

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
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with non-letter characters stripped."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("strip_non_alpha: 'columns' must be a non-empty list.")
        f = ctx.functions
        for column in columns:
            name = str(column)
            df = df.withColumn(name, f.regexp_replace(f.col(name), r"[^A-Za-z]", ""))
        return df


__all__ = [
    "RegexReplaceTransform",
    "StripNonAlphaTransform",
    "StripNonNumericTransform",
]
