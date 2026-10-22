"""Fix: Backfill null date fields with the harvest refresh date.

Summary:
    Fills in missing date values using the pipeline's harvest refresh date as
    a reasonable default.  When a date field is null (e.g., enrollment end
    date not yet recorded because the patient is still active), this fix
    replaces it with the corresponding harvest refresh date -- representing
    "at least through this date."  This is the inverse of
    ``future_date_to_null``: that fix nulls future dates, while this one
    backfills null dates.  Together they ensure date fields are either
    within the valid harvest window or explicitly null.

Use Cases:
    - Backfill null enrollment end dates with the harvest enrollment
      refresh date (patient is still enrolled as of the refresh).
      Config::

          {"fix": "null_date_to_harvest_refresh", "src1": "ENR_END_DATE", "src2": "REFRESH_ENROLLMENT_DATE"}

    - Backfill null enrollment start dates with the harvest refresh date.
      Config::

          {"fix": "null_date_to_harvest_refresh", "src1": "ENR_START_DATE", "src2": "REFRESH_ENROLLMENT_DATE"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Often paired with
       ``future_date_to_null`` on the same field -- run
       ``future_date_to_null`` first to null out future dates, then
       ``null_date_to_harvest_refresh`` to backfill any remaining nulls.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "enrollment": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "future_date_to_null", "src1": "ENR_END_DATE", "src2": "REFRESH_ENROLLMENT_DATE"},
                   {"fix": "null_date_to_harvest_refresh", "src1": "ENR_END_DATE", "src2": "REFRESH_ENROLLMENT_DATE"},
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
          temp view **before** the target table.
        - The harvest refresh field (``src2``) must exist in the harvest
          table and contain a valid date value.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, when

from ovid2.transformations.fixers.base import BaseFixer


class NullDateToHarvestRefreshFixer(BaseFixer):
    """Backfill null date values with the corresponding harvest refresh date.

    Reads the single-row ``mapped_harvest`` table from the input address to
    obtain the harvest refresh date.  Cross-joins the one-row harvest
    DataFrame (broadcast) with the main DataFrame, then replaces null values
    in the target date field with the harvest refresh value.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "null_date_to_harvest_refresh"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Replace null dates with the harvest refresh date.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Date field name to backfill (e.g.,
                  ``"ENR_END_DATE"``).
                - ``src2``: Harvest refresh field name (e.g.,
                  ``"REFRESH_ENROLLMENT_DATE"``).

        Returns:
            DataFrame with null dates replaced by the harvest refresh date.
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
                col(date_field).isNull(),
                col("_harvest_date"),
            ).otherwise(col(date_field)),
        ).drop("_harvest_date")

        return result
