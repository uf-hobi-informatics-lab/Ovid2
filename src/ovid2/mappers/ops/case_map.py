"""``case_map`` — apply upper/lower/title case to a column.

Params:
    column: Column to transform.
    mode: One of ``'upper'``, ``'lower'``, ``'title'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ovid2.config.capabilities import Capability
from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class CaseMapOp(BaseMapOp):
    """Apply upper/lower/title case to a column."""

    #: Engine capabilities this algorithm uses (checked by contract).
    REQUIRES: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.FN_COL,
            Capability.FN_INITCAP,
            Capability.FN_LOWER,
            Capability.FN_UPPER,
        }
    )

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "case_map"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Return ``df`` with the named column in the requested case."""
        from ovid2.core.engines.dsl import col as sf_col
        from ovid2.core.engines.dsl import initcap, lower, upper

        column = str(params["column"])
        mode = str(params.get("mode", "upper")).lower()

        if mode == "upper":
            expr = upper(sf_col(column))
        elif mode == "lower":
            expr = lower(sf_col(column))
        elif mode == "title":
            expr = initcap(sf_col(column))
        else:
            raise ValueError(
                f"case_map: unknown mode '{mode}'. Expected 'upper', 'lower', or 'title'."
            )
        return df.withColumn(column, expr)
