"""Fix: Null out date fields that are in the future relative to the harvest refresh date.

Summary:
    Prevents future-dated records from appearing in the dataset by nulling
    out date values that fall after the pipeline's harvest refresh date.  The
    harvest table contains per-domain refresh dates (e.g.,
    ``REFRESH_ENROLLMENT_DATE``, ``REFRESH_ENCOUNTER_DATE``) that represent
    the latest date for which the pipeline has complete data.  Dates beyond
    that boundary are likely data entry errors or forward-dated
    administrative entries.  The field is set to null rather than deleting
    the row, preserving the clinical record.

Use Cases:
    - Null out enrollment end dates that are after the harvest enrollment
      refresh date (e.g., an ENR_END_DATE of 2099-12-31 used as a sentinel
      for "still active").
      Config::

          {"fix": "future_date_to_null", "src1": "ENR_END_DATE", "src2": "REFRESH_ENROLLMENT_DATE"}

    - Null out encounter discharge dates that exceed the harvest encounter
      refresh date.
      Config::

          {"fix": "future_date_to_null", "src1": "DISCHARGE_DATE", "src2": "REFRESH_ENCOUNTER_DATE"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Typically placed after
       orphan fixes and replication.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "enrollment": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "future_date_to_null", "src1": "ENR_END_DATE", "src2": "REFRESH_ENROLLMENT_DATE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables enrollment --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``harvest`` table must be processed and registered as a Spark
          temp view **before** the target table.  The default table ordering
          in ``tables_keys.py`` places ``harvest`` third.
        - The harvest refresh field (``src2``) must exist in the harvest
          table and contain a valid date value.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, lit, to_date, when

from ovid2.transformations.fixers.base import BaseFixer


class FutureDateToNullFixer(BaseFixer):
    """Null out date values that fall after the harvest refresh date.

    Reads the single-row ``mapped_harvest`` table from the input address to
    obtain the harvest refresh date.  Cross-joins the one-row harvest
    DataFrame (broadcast) with the main DataFrame, then sets the target date
    field to null wherever it exceeds the harvest date.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "future_date_to_null"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Null out dates that are after the harvest refresh date.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Date field name to check (e.g., ``"ENR_END_DATE"``).
                - ``src2``: Harvest refresh field name (e.g.,
                  ``"REFRESH_ENROLLMENT_DATE"``).

        Returns:
            DataFrame with future dates set to null.
        """
        date_field: str = context["src1"]
        harvest_refresh_field: str = context["src2"]

        harvest_df: DataFrame = context["spark"].table("harvest")

        harvest_ref: DataFrame = harvest_df.select(
            col(harvest_refresh_field).alias("_harvest_date"),
        )

        joined: DataFrame = df.crossJoin(broadcast(harvest_ref))

        result: DataFrame = joined.withColumn(
            date_field,
            when(
                to_date(col(date_field)) > to_date(col("_harvest_date")),
                lit(None),
            ).otherwise(col(date_field)),
        ).drop("_harvest_date")

        return result
