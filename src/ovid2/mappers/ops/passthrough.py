"""``passthrough`` — explicit no-op.

Useful as a documented marker in a spec that a particular table
deliberately has no mapping work to do (versus "we forgot to add
mapping ops"). Also handy in tests as a minimal but non-empty op list.

Params: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class PassthroughOp(BaseMapOp):
    """Return the DataFrame unchanged."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "passthrough"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Return ``df`` unchanged."""
        return df
