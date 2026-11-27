"""``literal`` — assign a constant to a column.

Useful for CDM columns that a pipeline always fills with a fixed value
(source system name, harvest constants, etc.).

Params:
    column: Column to overwrite (created when absent).
    value: Constant value. Any JSON-compatible scalar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ovid2.config.capabilities import Capability
from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class LiteralOp(BaseMapOp):
    """Overwrite a column with a constant value."""

    #: Engine capabilities this algorithm uses (checked by contract).
    REQUIRES: ClassVar[frozenset[Capability]] = frozenset({Capability.FN_LIT})

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "literal"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Return ``df`` with the column set to ``params['value']``."""
        from ovid2.core.engines.dsl import lit

        column = str(params["column"])
        value = params.get("value")
        return df.withColumn(column, lit(value))
