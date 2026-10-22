"""Fix: Null out provider IDs that do not exist in the provider table.

Summary:
    Ensures referential integrity for provider-linked tables by setting
    orphan provider ID values to null.  Like ``encounterid_orphans``, this
    fix preserves the row and only nulls the provider field -- the clinical
    data remains but is unlinked from the provider.  The ``src2`` parameter
    specifies which column to check, since different tables use different
    provider field names (``PROVIDERID``, ``RX_PROVIDERID``, etc.).  If the
    provider reference table is missing (e.g., provider source file was not
    in the batch), the fix gracefully skips and returns the DataFrame
    unchanged.

Use Cases:
    - Null orphan provider IDs in the encounter table using the standard
      ``PROVIDERID`` column.
      Config::

          {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"}

    - Null orphan prescriber IDs in the prescribing table using the
      ``RX_PROVIDERID`` column (different field name for the same concept).
      Config::

          {"fix": "providerid_orphans", "src1": "provider", "src2": "RX_PROVIDERID"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place after
       ``patid_orphans`` and ``encounterid_orphans``.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"},
               ],
               "prescribing": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "providerid_orphans", "src1": "provider", "src2": "RX_PROVIDERID"},
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
        - The ``provider`` table must be processed and registered as a
          Spark temp view **before** the target table.  The default table
          ordering in ``tables_keys.py`` places ``provider`` second.
        - If the provider table was skipped (source file missing), the fix
          returns the DataFrame unchanged -- no error is raised.
"""

import logging
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, lit, when

from ovid2.transformations.fixers.base import BaseFixer

logger = logging.getLogger(__name__)


class ProvideridOrphansFixer(BaseFixer):
    """Null out provider ID values that have no match in the provider table.

    Performs a left join against the distinct PROVIDERIDs from the provider
    table specified in ``context['src1']``. The field to check is specified
    by ``context['src2']`` (e.g., ``"PROVIDERID"`` or ``"RX_PROVIDERID"``).
    Where the join does not match (orphan), the provider field is set to null.
    Rows are never deleted.

    If the provider reference table is not available as a Spark temp view
    (e.g., when the provider source file was missing and the table was
    skipped), the fix returns the DataFrame unchanged and logs a warning.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "providerid_orphans"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Null out provider IDs not present in the provider table.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Provider table name to read PROVIDERIDs from.
                - ``src2``: The provider field name to check in ``df``
                  (e.g., ``"PROVIDERID"`` or ``"RX_PROVIDERID"``).

        Returns:
            DataFrame with orphan provider IDs set to null, or the
            original DataFrame unchanged if the reference table is
            not available.
        """
        provider_table: str = context["src1"]
        provider_field: str = context["src2"]
        spark = context["spark"]

        # Check if the reference table exists as a temp view
        available_views = {t.name.lower() for t in spark.catalog.listTables() if t.isTemporary}
        if provider_table.lower() not in available_views:
            logger.warning(
                "providerid_orphans: reference table '%s' not found as temp "
                "view (was it skipped?). Returning DataFrame unchanged.",
                provider_table,
            )
            return df

        provider_df: DataFrame = spark.table(provider_table)

        valid_providers: DataFrame = provider_df.select(
            col("PROVIDERID").alias("_prov_ref_id"),
        ).distinct()

        joined: DataFrame = df.join(
            broadcast(valid_providers),
            on=(col(provider_field) == col("_prov_ref_id")),
            how="left",
        )

        result: DataFrame = joined.withColumn(
            provider_field,
            when(col("_prov_ref_id").isNull(), lit(None)).otherwise(col(provider_field)),
        ).drop("_prov_ref_id")

        return result
