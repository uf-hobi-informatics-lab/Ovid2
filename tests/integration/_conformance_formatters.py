"""Hand-written formatter used by the ``custom`` adapter conformance fixture."""

from __future__ import annotations

from typing import Any

from ovid2.formatters.base import BaseFormatter


class UpperFormatter(BaseFormatter):
    """Project ``patid`` and upper-cased ``sex`` from the demographic source."""

    @property
    def table_name(self) -> str:
        """Return the table name."""
        return "demographic"

    @property
    def input_file_name(self) -> str:
        """Return the input glob."""
        return "*Demographic_*.txt"

    def format(self, df: Any) -> Any:
        """Return the projected frame (lazy)."""
        from ovid2.core.engines.dsl import col, upper

        return df.select(col("patid"), upper(col("sex")).alias("sex"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("conformance fixture module.")
