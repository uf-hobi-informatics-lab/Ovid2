"""Fix: Delete records whose date is before the patient's birth date.

Summary:
    Removes records with clinically impossible temporal relationships --
    specifically, events dated before the patient was born.  These records
    are data entry errors or migration artifacts.  The fix joins against
    the demographic table to obtain each patient's BIRTH_DATE, then
    removes rows where the specified date field precedes that birth date.
    Patients with no BIRTH_DATE on file are not penalised (their records
    are kept).

Use Cases:
    - Remove encounter rows whose ADMIT_DATE is before the patient's
      birth date (e.g., a 2020 admit date for a patient born in 2023).
      Config::

          {"fix": "delete_records_before_birth", "src1": "ADMIT_DATE", "src2": "demographic"}

    - Remove diagnosis rows whose DX_DATE predates the patient's birth.
      Config::

          {"fix": "delete_records_before_birth", "src1": "DX_DATE", "src2": "demographic"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place after orphan and
       replication fixes (so ADMIT_DATE and PATIDs are correct).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "delete_records_before_birth", "src1": "ADMIT_DATE", "src2": "demographic"},
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
        - The ``demographic`` table (or the table named in ``src2``) must
          be processed and registered as a Spark temp view before the
          target table.
        - The date column in ``src1`` must exist in the DataFrame.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, to_date

from ovid2.transformations.fixers.base import BaseFixer


class DeleteRecordsBeforeBirthFixer(BaseFixer):
    """Remove records whose date field is earlier than the patient's birth date.

    Reads the demographic table specified in ``context['src2']`` to obtain
    BIRTH_DATE per PATID. Performs a left join on PATID, then keeps only rows
    where the date field (``context['src1']``) is on or after the patient's
    BIRTH_DATE — or where BIRTH_DATE is null (missing birth dates are not
    penalised).

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "delete_records_before_birth"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove records with a date before the patient's birth date.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Date field name to check in ``df``.
                - ``src2``: Demographic table name to read BIRTH_DATE from.

        Returns:
            DataFrame with pre-birth records removed.
        """
        date_field: str = context["src1"]
        demographic_table: str = context["src2"]

        demographic_df: DataFrame = context["spark"].table(demographic_table)

        birth_ref: DataFrame = demographic_df.select(
            col("PATID").alias("_demo_patid"),
            col("BIRTH_DATE").alias("_demo_birth_date"),
        )

        joined: DataFrame = df.join(
            broadcast(birth_ref),
            on=(col("PATID") == col("_demo_patid")),
            how="left",
        )

        result: DataFrame = joined.filter(
            col("_demo_birth_date").isNull()
            | (to_date(col(date_field)) >= to_date(col("_demo_birth_date")))
        ).drop("_demo_patid", "_demo_birth_date")

        return result
