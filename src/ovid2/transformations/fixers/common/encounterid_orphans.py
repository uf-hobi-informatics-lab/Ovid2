"""Fix: Null out ENCOUNTERIDs that do not exist in the encounter table.

Summary:
    Ensures referential integrity for encounter-linked tables by setting
    orphan ENCOUNTERID values to null.  Unlike ``patid_orphans`` which
    deletes rows, this fix **preserves** the row and only nulls out the
    ENCOUNTERID field -- the clinical data remains but is unlinked from
    any specific encounter.  This is appropriate because an encounter
    reference gap does not invalidate the clinical observation itself.

Use Cases:
    - Null orphan encounter IDs in the diagnosis table: diagnosis rows
      whose ENCOUNTERID does not appear in the encounter table have their
      ENCOUNTERID set to null while keeping the diagnosis record.
      Config::

          {"fix": "encounterid_orphans", "src1": "encounter"}

    - Applied broadly across clinical tables (vital, lab_result_cm,
      prescribing, med_admin, obs_clin, obs_gen, procedures, etc.).
      Config::

          {"fix": "encounterid_orphans", "src1": "encounter"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place after
       ``patid_orphans`` (orphan PATIDs should be removed first) and before
       ``replication`` (which joins on ENCOUNTERID).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "diagnosis": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "replication", "src1": "encounter"},
                   # ... other fixes ...
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables diagnosis --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``encounter`` table must be processed and registered as a
          Spark temp view **before** the target table.  The default table
          ordering in ``tables_keys.py`` ensures this.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, lit, when

from ovid2.transformations.fixers.base import BaseFixer


class EncounteridOrphansFixer(BaseFixer):
    """Null out ENCOUNTERID values that have no match in the encounter table.

    Performs a left join against the distinct ENCOUNTERIDs from the encounter
    table specified in ``context['src1']``. Where the join does not match
    (orphan), the ENCOUNTERID is set to null. Rows are never deleted.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "encounterid_orphans"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Null out ENCOUNTERIDs not present in the encounter table.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Encounter table name to read ENCOUNTERIDs from.

        Returns:
            DataFrame with orphan ENCOUNTERIDs set to null.
        """
        encounter_table: str = context["src1"]

        encounter_df: DataFrame = context["spark"].table(encounter_table)

        valid_encounters: DataFrame = encounter_df.select(
            col("ENCOUNTERID").alias("_enc_ref_id"),
        ).distinct()

        joined: DataFrame = df.join(
            broadcast(valid_encounters),
            on=(col("ENCOUNTERID") == col("_enc_ref_id")),
            how="left",
        )

        result: DataFrame = joined.withColumn(
            "ENCOUNTERID",
            when(col("_enc_ref_id").isNull(), lit(None)).otherwise(col("ENCOUNTERID")),
        ).drop("_enc_ref_id")

        return result
