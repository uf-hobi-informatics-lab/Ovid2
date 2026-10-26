"""``regex_replace`` — substitute a regex pattern in one column.

Params:
    column: Column to transform.
    pattern: Regex pattern to match.
    replacement: Replacement string (may reference groups as ``$1`` etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class RegexReplaceOp(BaseMapOp):
    """Regex substitution on one column."""

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
        from pyspark.sql.functions import col as sf_col
        from pyspark.sql.functions import regexp_replace

        column = str(params["column"])
        pattern = str(params["pattern"])
        replacement = str(params.get("replacement", ""))
        return df.withColumn(column, regexp_replace(sf_col(column), pattern, replacement))
