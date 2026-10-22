"""example_pcornet death_cause formatter — explodes comma-separated DEATH_CAUSE values.

example_pcornet sends multiple death causes as a single comma-separated string. This
formatter splits them into individual rows and renames source columns to
match the PCORnet CDM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


INPUT_FILE_NAME: str = "*Death_Cause_*.txt"


class Death_causeFormatter(BaseFormatter):
    """Formatter for the example_pcornet ``death_cause`` table.

    Explodes comma-separated DEATH_CAUSE values into one row each, trims
    whitespace, and renames ``DEATH_SOURCE`` / ``DEATH_MATCH_CONFIDENCE``
    to their CDM counterparts.
    """

    @property
    def table_name(self) -> str:
        """Return the CDM table name."""
        return "death_cause"

    @property
    def input_file_name(self) -> str:
        """Return the input file glob."""
        return INPUT_FILE_NAME

    def format(self, df: "DataFrame") -> "DataFrame":  # noqa: A003 — CDM API
        """Reshape raw example_pcornet data into PCORnet death_cause format (lazy)."""
        from pyspark.sql.functions import array, col, explode, split, trim, when

        exploded = df.withColumn(
            "DEATH_CAUSE",
            explode(
                when(
                    col("DEATH_CAUSE").isNotNull(),
                    split(col("DEATH_CAUSE"), ","),
                ).otherwise(array(col("DEATH_CAUSE")))
            ),
        ).withColumn("DEATH_CAUSE", trim(col("DEATH_CAUSE")))

        return exploded.select(
            exploded["PATID"].alias("PATID"),
            exploded["DEATH_CAUSE"].alias("DEATH_CAUSE"),
            exploded["DEATH_CAUSE_CODE"].alias("DEATH_CAUSE_CODE"),
            exploded["DEATH_CAUSE_TYPE"].alias("DEATH_CAUSE_TYPE"),
            exploded["DEATH_SOURCE"].alias("DEATH_CAUSE_SOURCE"),
            exploded["DEATH_MATCH_CONFIDENCE"].alias("DEATH_CAUSE_CONFIDENCE"),
        )
