"""Data-correction transforms: null_if / clamp / standardize_null /
dedup / distinct / filter_expr.

CDM-agnostic cleanups. All lazy; ``dedup``/``distinct`` use the portable
``dropDuplicates``/``distinct`` DataFrame API, ``filter_expr`` uses a
column predicate, never string-formatted data.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


class NullIfTransform(BaseTransform):
    """Set a column to null when its value matches a condition.

    Supports three mutually-combinable conditions:

    * ``equals``: list of literal values that become null.
    * ``pattern``: regex — a full match becomes null.
    * ``less_than`` / ``greater_than``: numeric bounds.
    """

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "null_if"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Set a column to null when its value matches a list, pattern, or bound."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "equals": {"type": "array", "items": {}},
                "pattern": {"type": "string"},
                "less_than": {"type": "number"},
                "greater_than": {"type": "number"},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with matching values in the column nulled."""
        column = str(params["column"])
        f = ctx.functions
        col = f.col(column)

        condition = None

        def _or(existing: Any, term: Any) -> Any:
            return term if existing is None else (existing | term)

        equals = params.get("equals")
        if isinstance(equals, list) and equals:
            condition = _or(condition, col.isin(list(equals)))
        pattern = params.get("pattern")
        if pattern is not None:
            # rlike is a portable column predicate on both families.
            condition = _or(condition, col.rlike(str(pattern)))
        less_than = params.get("less_than")
        if less_than is not None:
            condition = _or(condition, col < f.lit(less_than))
        greater_than = params.get("greater_than")
        if greater_than is not None:
            condition = _or(condition, col > f.lit(greater_than))

        if condition is None:
            raise ValueError(
                "null_if: at least one of 'equals', 'pattern', 'less_than', "
                "'greater_than' must be provided."
            )
        return df.withColumn(column, f.when(condition, f.lit(None)).otherwise(col))


class ClampTransform(BaseTransform):
    """Bound a numeric column to ``[min, max]``.

    Values below ``min`` become ``min``; values above ``max`` become
    ``max``. Either bound may be omitted.
    """

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "clamp"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Bound a numeric column to a [min, max] range."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "min": {"type": "number"},
                "max": {"type": "number"},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the column clamped."""
        column = str(params["column"])
        lo = params.get("min")
        hi = params.get("max")
        if lo is None and hi is None:
            raise ValueError("clamp: at least one of 'min'/'max' must be provided.")
        f = ctx.functions
        col = f.col(column)
        if lo is not None:
            col = f.when(f.col(column) < f.lit(lo), f.lit(lo)).otherwise(col)
        if hi is not None:
            col = f.when(f.col(column) > f.lit(hi), f.lit(hi)).otherwise(col)
        return df.withColumn(column, col)


class StandardizeNullTransform(BaseTransform):
    """Map a set of sentinel strings to true null across columns.

    Default sentinels: ``""``, ``"NULL"``, ``"NA"``, ``"N/A"``, ``"."``.
    Comparison is case-insensitive after trimming.
    """

    category = "correction"

    _DEFAULT_SENTINELS = ("", "NULL", "NA", "N/A", ".")

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "standardize_null"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Convert sentinel strings (NULL, NA, '.', empty) to true null."

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
                },
                "sentinels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Override the default sentinel list.",
                },
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with sentinel values converted to null."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("standardize_null: 'columns' must be a non-empty list.")
        sentinels = params.get("sentinels")
        if sentinels is None:
            sentinels = list(self._DEFAULT_SENTINELS)
        upper_sentinels = [str(s).upper() for s in sentinels]
        f = ctx.functions
        for column in columns:
            name = str(column)
            normalized = f.upper(f.trim(f.col(name)))
            df = df.withColumn(
                name,
                f.when(normalized.isin(upper_sentinels), f.lit(None)).otherwise(f.col(name)),
            )
        return df


class DedupTransform(BaseTransform):
    """Drop duplicate rows, optionally on a subset of columns."""

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "dedup"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Drop duplicate rows (optionally keyed on a subset of columns)."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subset": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Columns that define uniqueness (omit for full-row).",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with duplicates dropped (lazy)."""
        subset = params.get("subset")
        if subset:
            return df.dropDuplicates([str(c) for c in subset])
        return df.dropDuplicates()


class DistinctTransform(BaseTransform):
    """Full-row distinct."""

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "distinct"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Keep only distinct rows (full-row uniqueness)."

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return distinct rows (lazy)."""
        return df.distinct()


class FilterExprTransform(BaseTransform):
    """Keep rows matching a boolean column expression.

    The expression is a Spark/Snowpark column predicate (e.g.
    ``AGE >= 0 AND AGE <= 120``). It is a *filter predicate*, not
    a query that reads external data, so it does not trip the
    parameterised-SQL rule.
    """

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "filter_expr"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Keep only rows matching a boolean column expression."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["expr"],
            "additionalProperties": False,
            "properties": {
                "expr": {
                    "type": "string",
                    "description": "Boolean column predicate, e.g. 'AGE >= 0'.",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` filtered by the predicate (lazy)."""
        predicate = str(params["expr"])
        f = ctx.functions
        return df.where(f.expr(predicate))


__all__ = [
    "ClampTransform",
    "DedupTransform",
    "DistinctTransform",
    "FilterExprTransform",
    "NullIfTransform",
    "StandardizeNullTransform",
]
