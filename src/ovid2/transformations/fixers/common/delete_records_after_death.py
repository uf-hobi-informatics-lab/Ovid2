"""Fix: Delete records whose date is after the patient's death date.

Summary:
    Removes records with clinically impossible temporal relationships --
    specifically, events dated after the patient's recorded death.  These
    records are data entry errors, delayed processing artifacts, or
    identity mismatches.  The fix joins against the death table to obtain
    each patient's DEATH_DATE, then removes rows where the specified date
    field is later than that death date.  Patients with no DEATH_DATE on
    file are not penalised (their records are kept).

Use Cases:
    - Remove encounter rows whose ADMIT_DATE is after the patient's
      recorded death date.
      Config::

          {"fix": "delete_records_after_death", "src1": "ADMIT_DATE", "src2": "death"}

    - Remove lab_result_cm rows whose RESULT_DATE postdates the patient's
      death.
      Config::

          {"fix": "delete_records_after_death", "src1": "RESULT_DATE", "src2": "death"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place after orphan and
       replication fixes.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "delete_records_after_death", "src1": "ADMIT_DATE", "src2": "death"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables encounter --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``death`` table (or the table named in ``src2``) must be
          processed and registered as a Spark temp view before the target
          table.
        - The date column in ``src1`` must exist in the DataFrame.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, to_date

from ovid2.transformations.fixers.base import BaseFixer


class DeleteRecordsAfterDeathFixer(BaseFixer):
    """Remove records whose date field is later than the patient's death date.

    Reads the death table specified in ``context['src2']`` to obtain
    DEATH_DATE per PATID. Performs a left join on PATID, then keeps only rows
    where the date field (``context['src1']``) is on or before the patient's
    DEATH_DATE — or where DEATH_DATE is null (missing death dates are not
    penalised).

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "delete_records_after_death"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove records with a date after the patient's death date.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Date field name to check in ``df``.
                - ``src2``: Death table name to read DEATH_DATE from.

        Returns:
            DataFrame with post-death records removed.
        """
        date_field: str = context["src1"]
        death_table: str = context["src2"]

        death_df: DataFrame = context["spark"].table(death_table)

        death_ref: DataFrame = death_df.select(
            col("PATID").alias("_death_patid"),
            col("DEATH_DATE").alias("_death_death_date"),
        )

        joined: DataFrame = df.join(
            broadcast(death_ref),
            on=(col("PATID") == col("_death_patid")),
            how="left",
        )

        result: DataFrame = joined.filter(
            col("_death_death_date").isNull()
            | (to_date(col(date_field)) <= to_date(col("_death_death_date")))
        ).drop("_death_patid", "_death_death_date")

        return result
