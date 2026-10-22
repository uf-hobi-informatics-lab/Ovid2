"""Fix: Backfill date fields from ADMIT_DATE, then delete rows still missing dates.

Summary:
    A two-phase fix for tables where date fields are required but sometimes
    null.  Phase 1: for each listed date field, null values are backfilled
    with the encounter's ADMIT_DATE (via a join on ENCOUNTERID).  Phase 2:
    any rows where **any** of the listed date fields is still null after
    backfilling are deleted.  This ensures the output has no null dates in
    the specified fields while preserving as many records as possible by
    using ADMIT_DATE as a reasonable fallback.  Commonly used for
    med_admin, obs_clin, obs_gen, and immunization tables where start/stop
    dates are required by the CDM.

Use Cases:
    - Backfill med_admin start and stop dates from ADMIT_DATE, then remove
      rows where dates are still missing (no encounter match either).
      Config::

          {"fix": "copy_admit_date_or_delete", "src1": "MEDADMIN_START_DATE,MEDADMIN_STOP_DATE", "src2": "encounter"}

    - Backfill immunization date from ADMIT_DATE, delete if still null.
      Config::

          {"fix": "copy_admit_date_or_delete", "src1": "VX_ADMIN_DATE", "src2": "encounter"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place after orphan and
       replication fixes.  Multiple date fields are passed as a
       comma-separated string in ``src1``.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "med_admin": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "copy_admit_date_or_delete", "src1": "MEDADMIN_START_DATE,MEDADMIN_STOP_DATE", "src2": "encounter"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables med_admin --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``encounter`` table (or the table named in ``src2``) must be
          processed and registered as a Spark temp view before the target
          table.
        - All date field names in the comma-separated ``src1`` must exist
          in the DataFrame.
"""

from functools import reduce
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, when

from ovid2.transformations.fixers.base import BaseFixer


class CopyAdmitDateOrDeleteFixer(BaseFixer):
    """Backfill null date fields with ADMIT_DATE from the encounter table.

    Reads the encounter table to obtain ADMIT_DATE via a left join on
    ENCOUNTERID.  For each date field in the comma-separated list
    (``context['src1']``), null values are replaced with the encounter's
    ADMIT_DATE.  After backfilling, any rows where **any** of the listed
    date fields is still null are removed.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "copy_admit_date_or_delete"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Backfill dates from ADMIT_DATE and delete rows with remaining nulls.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Comma-separated list of date field names to
                  backfill (e.g., ``"MEDADMIN_START_DATE,MEDADMIN_STOP_DATE"``).
                - ``src2``: Encounter table name to read ADMIT_DATE from.

        Returns:
            DataFrame with dates backfilled and null-date rows removed.
        """
        date_fields: list[str] = [field.strip() for field in context["src1"].split(",")]
        encounter_table: str = context["src2"]

        encounter_df: DataFrame = context["spark"].table(encounter_table)

        encounter_ref: DataFrame = encounter_df.select(
            col("ENCOUNTERID").alias("_enc_ref_id"),
            col("ADMIT_DATE").alias("_enc_admit_date"),
        )

        joined: DataFrame = df.join(
            broadcast(encounter_ref),
            on=(col("ENCOUNTERID") == col("_enc_ref_id")),
            how="left",
        )

        # Backfill each date field with ADMIT_DATE where null
        backfilled: DataFrame = joined
        for date_field in date_fields:
            backfilled = backfilled.withColumn(
                date_field,
                when(col(date_field).isNull(), col("_enc_admit_date")).otherwise(col(date_field)),
            )

        # Drop temporary columns
        backfilled = backfilled.drop("_enc_ref_id", "_enc_admit_date")

        # Delete rows where ANY of the date fields is still null
        not_null_condition = reduce(
            lambda a, b: a & b,
            [col(field).isNotNull() for field in date_fields],
        )

        result: DataFrame = backfilled.filter(not_null_condition)

        return result
