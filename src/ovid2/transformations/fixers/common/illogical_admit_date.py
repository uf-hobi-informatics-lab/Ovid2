"""Fix: Replace dates that are illogically before the admit date.

Summary:
    Corrects date fields that are more than 5 days before the encounter's
    ADMIT_DATE by overwriting them with ADMIT_DATE.  A small tolerance
    window (5 days) accounts for pre-admission testing, registration, or
    minor date-entry discrepancies.  Dates more than 5 days early are
    likely data errors (e.g., a procedure date of 2020-01-01 on a 2024
    encounter).  The row is preserved -- only the date value is corrected.
    This fix requires that ADMIT_DATE already be present in the DataFrame,
    typically via a prior ``replication`` fix.

Use Cases:
    - Correct procedure dates that are more than 5 days before the
      encounter admit date.
      Config::

          {"fix": "illogical_admit_date", "src1": "PX_DATE"}

    - Correct diagnosis dates that are illogically early.
      Config::

          {"fix": "illogical_admit_date", "src1": "DX_DATE"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place **after**
       ``replication`` (which populates ADMIT_DATE from the encounter
       table) and after orphan fixes.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "procedures": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "replication", "src1": "encounter"},
                   {"fix": "illogical_admit_date", "src1": "PX_DATE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables procedures --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The DataFrame must contain an ``ADMIT_DATE`` column.  Run the
          ``replication`` fix first to populate it from the encounter table.
        - The date column in ``src1`` must exist in the DataFrame.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, datediff, to_date, when

from ovid2.transformations.fixers.base import BaseFixer


class IllogicalAdmitDateFixer(BaseFixer):
    """Replace date values that are more than 5 days before ADMIT_DATE.

    The input DataFrame must already contain an ``ADMIT_DATE`` column (e.g.,
    via a prior replication fix).  Where the target date field is more than
    5 days before ADMIT_DATE, it is overwritten with ADMIT_DATE.  Rows are
    never deleted.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "illogical_admit_date"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Replace dates that are more than 5 days before ADMIT_DATE.

        The comparison is:
        ``datediff(to_date(ADMIT_DATE), to_date(src1)) > 5``
        meaning the field value is more than 5 days **before** the admit date.

        Args:
            df: The DataFrame to fix.  Must contain an ``ADMIT_DATE`` column.
            context: Dictionary containing:
                - ``src1``: Date field name to examine and potentially replace.

        Returns:
            DataFrame with illogically early dates replaced by ADMIT_DATE.
        """
        date_field: str = context["src1"]

        result: DataFrame = df.withColumn(
            date_field,
            when(
                datediff(to_date(col("ADMIT_DATE")), to_date(col(date_field))) > 5,
                col("ADMIT_DATE"),
            ).otherwise(col(date_field)),
        )

        return result
