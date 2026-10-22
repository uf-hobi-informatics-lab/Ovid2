"""``to_upper`` / ``to_lower`` / ``to_title`` — case-normalise columns.

Params:
    columns: List of column names to transform.

Each op rewrites the named columns in place using the portable
``EngineFunctions`` DSL so it runs under any compute family.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext

_COLUMNS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["columns"],
    "additionalProperties": False,
    "properties": {
        "columns": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "Columns to case-normalise (rewritten in place).",
        }
    },
}


def _columns(params: dict[str, Any]) -> list[str]:
    """Return the validated ``columns`` list."""
    columns = params.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("case transform: 'columns' must be a non-empty list.")
    return [str(c) for c in columns]


class ToUpperTransform(BaseTransform):
    """Uppercase one or more string columns."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "to_upper"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Uppercase the values in one or more columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return _COLUMNS_SCHEMA

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns uppercased."""
        f = ctx.functions
        for column in _columns(params):
            df = df.withColumn(column, f.upper(f.col(column)))
        return df


class ToLowerTransform(BaseTransform):
    """Lowercase one or more string columns."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "to_lower"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Lowercase the values in one or more columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return _COLUMNS_SCHEMA

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns lowercased."""
        f = ctx.functions
        for column in _columns(params):
            df = df.withColumn(column, f.lower(f.col(column)))
        return df


class ToTitleTransform(BaseTransform):
    """Title-case (initcap) one or more string columns.

    Uses the portable ``expr`` DSL entry to reach ``initcap`` without
    binding to any concrete engine module.
    """

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "to_title"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Title-case (initcap) the values in one or more columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return _COLUMNS_SCHEMA

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns title-cased."""
        f = ctx.functions
        for column in _columns(params):
            df = df.withColumn(column, f.expr(f"initcap(`{column}`)"))
        return df


__all__ = ["ToLowerTransform", "ToTitleTransform", "ToUpperTransform"]
