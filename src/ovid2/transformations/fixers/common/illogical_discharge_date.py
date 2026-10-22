"""Fix: Replace dates that are illogically after the discharge date.

Summary:
    Corrects date fields that are more than 5 days after the encounter's
    DISCHARGE_DATE by overwriting them with DISCHARGE_DATE.  A small
    tolerance window (5 days) accounts for post-discharge follow-up orders,
    delayed result entry, or minor date-entry discrepancies.  Dates more
    than 5 days late are likely data errors (e.g., a procedure date of
    2025-06-01 on an encounter discharged 2024-01-15).  The row is
    preserved -- only the date value is corrected.  Unlike
    ``illogical_admit_date``, this fix reads DISCHARGE_DATE from the
    encounter table via a join rather than requiring it in the DataFrame.

Use Cases:
    - Correct procedure dates that are more than 5 days after the
      encounter discharge date.
      Config::

          {"fix": "illogical_discharge_date", "src1": "PX_DATE", "src2": "encounter"}

    - Correct prescribing order dates that are illogically late.
      Config::

          {"fix": "illogical_discharge_date", "src1": "RX_ORDER_DATE", "src2": "encounter"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place **after**
       ``replication`` and ``illogical_admit_date`` (if both are used).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "procedures": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "replication", "src1": "encounter"},
                   {"fix": "illogical_admit_date", "src1": "PX_DATE"},
                   {"fix": "illogical_discharge_date", "src1": "PX_DATE", "src2": "encounter"},
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
        - The ``encounter`` table (or the table named in ``src2``) must be
          processed and registered as a Spark temp view before the target
          table.
        - The date column in ``src1`` must exist in the DataFrame.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, datediff, to_date, when

from ovid2.transformations.fixers.base import BaseFixer


class IllogicalDischargeDateFixer(BaseFixer):
    """Replace date values that are more than 5 days after DISCHARGE_DATE.

    Reads the encounter table to obtain DISCHARGE_DATE via a left join on
    ENCOUNTERID.  Where the target date field is more than 5 days after the
    discharge date, it is overwritten with DISCHARGE_DATE.  Rows are never
    deleted.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "illogical_discharge_date"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Replace dates that are more than 5 days after DISCHARGE_DATE.

        The comparison is:
        ``datediff(to_date(src1), to_date(DISCHARGE_DATE)) > 5``
        meaning the field value is more than 5 days **after** the discharge date.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Date field name to examine and potentially replace.
                - ``src2``: Encounter table name to read DISCHARGE_DATE from.

        Returns:
            DataFrame with illogically late dates replaced by DISCHARGE_DATE.
        """
        date_field: str = context["src1"]
        encounter_table: str = context["src2"]

        encounter_df: DataFrame = context["spark"].table(encounter_table)

        encounter_ref: DataFrame = encounter_df.select(
            col("ENCOUNTERID").alias("_enc_ref_id"),
            col("DISCHARGE_DATE").alias("_enc_discharge_date"),
        )

        joined: DataFrame = df.join(
            encounter_ref,
            on=(col("ENCOUNTERID") == col("_enc_ref_id")),
            how="left",
        )

        result: DataFrame = joined.withColumn(
            date_field,
            when(
                datediff(
                    to_date(col(date_field)),
                    to_date(col("_enc_discharge_date")),
                )
                > 5,
                col("_enc_discharge_date"),
            ).otherwise(col(date_field)),
        ).drop("_enc_ref_id", "_enc_discharge_date")

        return result
