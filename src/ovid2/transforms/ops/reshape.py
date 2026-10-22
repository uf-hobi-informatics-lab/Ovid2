"""Reshape transforms: reduce / pivot / unpivot / explode /
add_row_number / rank / dense_rank.

These change the row/column shape of a DataFrame. Aggregations and the
group-by ``reduce`` use the portable DataFrame API. Window-based ops
(``add_row_number``, ``rank``, ``dense_rank``) declare
``requires_family = "spark"`` because Snowpark's window idiom diverges
enough that we gate rather than silently mis-run.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


#: Aggregation functions accepted by ``reduce`` / ``head_by_group``.
_AGG_FUNCS: frozenset[str] = frozenset(
    {"max", "min", "sum", "avg", "mean", "count", "first", "last"}
)


class ReduceTransform(BaseTransform):
    """Group-by + aggregation — the classic reduce.

    Params:
        group_by: Columns to group on.
        aggs: ``{column: agg_func}`` map. ``agg_func`` is one of
            max/min/sum/avg/mean/count/first/last.
    """

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "reduce"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Group by key columns and aggregate the rest (one row per group)."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["group_by", "aggs"],
            "additionalProperties": False,
            "properties": {
                "group_by": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "aggs": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "string",
                        "enum": sorted(_AGG_FUNCS),
                    },
                    "description": "{column: agg_func} — max/min/sum/avg/count/first/last.",
                },
            },
        }

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return the grouped-and-aggregated DataFrame (lazy)."""
        group_by = params.get("group_by")
        aggs = params.get("aggs")
        if not isinstance(group_by, list) or not group_by:
            raise ValueError("reduce: 'group_by' must be a non-empty list.")
        if not isinstance(aggs, dict) or not aggs:
            raise ValueError("reduce: 'aggs' must be a non-empty object.")
        agg_map: dict[str, str] = {}
        for column, func in aggs.items():
            fn = str(func).lower()
            if fn not in _AGG_FUNCS:
                raise ValueError(f"reduce: unsupported agg func '{func}' for column '{column}'.")
            agg_map[str(column)] = fn
        grouped = df.groupBy(*[str(c) for c in group_by])
        # ``DataFrame.groupBy().agg({...})`` is portable and accepts the
        # {column: func_name} form on both PySpark and Snowpark.
        return grouped.agg(agg_map)


class ExplodeTransform(BaseTransform):
    """Explode an array/map column into one row per element."""

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "explode"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Expand an array or map column into one row per element."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "alias": {
                    "type": "string",
                    "description": "Name for the exploded value (defaults to the column name).",
                },
            },
        }

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return ``df`` with the column exploded (lazy)."""
        column = str(params["column"])
        alias = str(params.get("alias", column))
        f = ctx.functions
        # explode via the portable expr entry; keep all other columns.
        others = [c for c in df.columns if c != column]
        select_cols = [f.col(c) for c in others]
        select_cols.append(f.expr(f"explode(`{column}`)").alias(alias))
        return df.select(*select_cols)


class UnpivotTransform(BaseTransform):
    """Melt wide value columns into (name, value) long form.

    Implemented as a portable union of per-column projections so it runs
    on any family without relying on a native ``melt``/``unpivot``.
    """

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "unpivot"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Melt wide value columns into long (name, value) rows."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["id_columns", "value_columns", "name_to", "value_to"],
            "additionalProperties": False,
            "properties": {
                "id_columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "value_columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "name_to": {"type": "string"},
                "value_to": {"type": "string"},
            },
        }

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return the long-form DataFrame (lazy)."""
        id_columns = [str(c) for c in params["id_columns"]]
        value_columns = [str(c) for c in params["value_columns"]]
        name_to = str(params["name_to"])
        value_to = str(params["value_to"])
        if not value_columns:
            raise ValueError("unpivot: 'value_columns' must be non-empty.")
        f = ctx.functions
        parts = []
        for vc in value_columns:
            projection = [f.col(c) for c in id_columns]
            projection.append(f.lit(vc).alias(name_to))
            projection.append(f.col(vc).alias(value_to))
            parts.append(df.select(*projection))
        result = parts[0]
        for part in parts[1:]:
            result = result.unionByName(part)
        return result


class PivotTransform(BaseTransform):
    """Pivot long form to wide via group-by + pivot + aggregation."""

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "pivot"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Pivot a key/value column pair into wide columns per key."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["group_by", "pivot_column", "value_column"],
            "additionalProperties": False,
            "properties": {
                "group_by": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "pivot_column": {"type": "string"},
                "value_column": {"type": "string"},
                "agg": {"type": "string", "enum": sorted(_AGG_FUNCS), "default": "first"},
            },
        }

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return the wide DataFrame (lazy)."""
        group_by = [str(c) for c in params["group_by"]]
        pivot_column = str(params["pivot_column"])
        value_column = str(params["value_column"])
        agg = str(params.get("agg", "first")).lower()
        if agg not in _AGG_FUNCS:
            raise ValueError(f"pivot: unsupported agg func '{agg}'.")
        grouped = df.groupBy(*group_by).pivot(pivot_column)
        return grouped.agg({value_column: agg})


class AddRowNumberTransform(BaseTransform):
    """Add a 1-based row number within partitions, ordered by columns."""

    category = "reshape"
    requires_family = "spark"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "add_row_number"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Add a 1-based row_number within partitions, ordered by columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["order_by", "output_column"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {"type": "array", "items": {"type": "string"}},
                "order_by": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["col"],
                        "properties": {
                            "col": {"type": "string"},
                            "desc": {"type": "boolean", "default": False},
                        },
                    },
                },
                "output_column": {"type": "string"},
            },
        }

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return ``df`` with the row-number column added (lazy)."""
        from ovid2.transforms.ops._window import build_window, row_number_expr

        output_column = str(params["output_column"])
        window = build_window(params, ctx)
        return df.withColumn(output_column, row_number_expr(ctx).over(window))


class RankTransform(BaseTransform):
    """Add a rank (with gaps) within partitions."""

    category = "reshape"
    requires_family = "spark"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "rank"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Add a rank (with gaps for ties) within partitions."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return AddRowNumberTransform().params_schema

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return ``df`` with the rank column added (lazy)."""
        from ovid2.transforms.ops._window import build_window, rank_expr

        output_column = str(params["output_column"])
        window = build_window(params, ctx)
        return df.withColumn(output_column, rank_expr(ctx).over(window))


class DenseRankTransform(BaseTransform):
    """Add a dense rank (no gaps) within partitions."""

    category = "reshape"
    requires_family = "spark"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "dense_rank"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Add a dense rank (no gaps for ties) within partitions."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return AddRowNumberTransform().params_schema

    def apply(
        self, df: "DataFrame", params: dict[str, Any], ctx: TransformContext
    ) -> "DataFrame":
        """Return ``df`` with the dense-rank column added (lazy)."""
        from ovid2.transforms.ops._window import build_window, dense_rank_expr

        output_column = str(params["output_column"])
        window = build_window(params, ctx)
        return df.withColumn(output_column, dense_rank_expr(ctx).over(window))


__all__ = [
    "AddRowNumberTransform",
    "DenseRankTransform",
    "ExplodeTransform",
    "PivotTransform",
    "RankTransform",
    "ReduceTransform",
    "UnpivotTransform",
]
