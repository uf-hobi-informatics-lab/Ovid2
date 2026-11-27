"""``regex_replace`` — substitute a regex pattern in one column.

Params:
    column: Column to transform.
    pattern: Regex pattern to match.
    replacement: Replacement string (may reference groups as ``$1`` etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ovid2.config.capabilities import Capability
from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class RegexReplaceOp(BaseMapOp):
    """Regex substitution on one column."""

    #: Engine capabilities this algorithm uses (checked by contract).
    REQUIRES: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.FN_COL,
            Capability.FN_REGEXP_REPLACE,
            Capability.REGEX,
        }
    )

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "regex_replace"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Return ``df`` with the regex substitution applied to the column."""
        from ovid2.core.engines.dsl import col as sf_col
        from ovid2.core.engines.dsl import regexp_replace

        column = str(params["column"])
        pattern = str(params["pattern"])
        replacement = str(params.get("replacement", ""))
        return df.withColumn(column, regexp_replace(sf_col(column), pattern, replacement))
