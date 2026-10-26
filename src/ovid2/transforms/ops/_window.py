"""Portable window-expression helpers for reshape/sampling ops.

Window functions have no entry on :class:`EngineFunctions` and we may
not ``import pyspark`` in this tree. Instead we express windows as SQL
fragments through the portable ``functions.expr`` entry, which both the
PySpark and (future) Snowpark ``EngineFunctions`` adapters implement.
The reshape/sampling ops that use these helpers declare
``requires_family = "spark"`` so the engine gates them; the SQL used
here is ANSI window syntax that Spark SQL accepts.

The expression built is, for example::

    row_number() OVER (PARTITION BY `SITE` ORDER BY `DT` DESC)

Column and function tokens are back-quoted / whitelisted; no external
data is interpolated, so this does not trip the parameterised-SQL rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.transforms.base import TransformContext


def _over_clause(params: dict[str, Any]) -> str:
    """Build the ``OVER (...)`` clause from ``partition_by``/``order_by``."""
    parts: list[str] = []
    partition_by = params.get("partition_by") or []
    if partition_by:
        cols = ", ".join(f"`{c!s}`" for c in partition_by)
        parts.append(f"PARTITION BY {cols}")
    order_by = params.get("order_by") or []
    if order_by:
        terms: list[str] = []
        for entry in order_by:
            col = str(entry["col"])
            direction = "DESC" if bool(entry.get("desc", False)) else "ASC"
            terms.append(f"`{col}` {direction}")
        parts.append("ORDER BY " + ", ".join(terms))
    return " ".join(parts)


class _WindowExpr:
    """Tiny adapter so callers can write ``expr.over(window)``.

    Mirrors the PySpark ``Column.over(Window)`` call shape used by the
    reshape ops, but is engine-portable: ``over`` folds the window
    clause into a single ``functions.expr`` call.
    """

    def __init__(self, ctx: TransformContext, func_sql: str) -> None:
        self._ctx = ctx
        self._func_sql = func_sql

    def over(self, over_clause: str) -> Any:
        """Return the portable column expression for ``func OVER (...)``."""
        return self._ctx.functions.expr(f"{self._func_sql} OVER ({over_clause})")


def build_window(params: dict[str, Any], ctx: TransformContext) -> str:
    """Return the ``OVER (...)`` inner clause string for these params."""
    _ = ctx  # signature symmetry with the *_expr helpers
    return _over_clause(params)


def row_number_expr(ctx: TransformContext) -> _WindowExpr:
    """Return a ``row_number()`` window expression builder."""
    return _WindowExpr(ctx, "row_number()")


def rank_expr(ctx: TransformContext) -> _WindowExpr:
    """Return a ``rank()`` window expression builder."""
    return _WindowExpr(ctx, "rank()")


def dense_rank_expr(ctx: TransformContext) -> _WindowExpr:
    """Return a ``dense_rank()`` window expression builder."""
    return _WindowExpr(ctx, "dense_rank()")


__all__ = [
    "build_window",
    "dense_rank_expr",
    "rank_expr",
    "row_number_expr",
]
