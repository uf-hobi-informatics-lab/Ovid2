"""Fix: Add stub hash_token rows for PATIDs missing from the hash_token table.

Summary:
    Ensures every patient in the demographic table has a corresponding row
    in the hash_token table, which is required for patient linkage across
    a research network.  Some pipelines may not include hash tokens for
    all patients (e.g., patients added after the token generation run).
    This fix finds PATIDs present in demographic but missing from
    hash_token, creates stub rows with null token values, and appends them.
    The stub rows include a current timestamp for ``UPDATED`` and the
    pipeline name for ``SOURCE``.  Applied exclusively to the ``hash_token``
    table.

Use Cases:
    - Add stub hash_token rows for patients missing from the hash_token
      extract, referencing the demographic table for the complete PATID
      list.
      Config::

          {"fix": "add_missing_hash_token_patids", "src1": "demographic"}

Deployment:
    1. Add the fix dict to the ``hash_token`` table's entry in
       ``FIXER_CHAINS`` in ``pipelines/<pipeline>/fixer_configs.py``.
       Typically the last fix for hash_token (after ``patid_orphans`` if
       present).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "hash_token": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "add_missing_hash_token_patids", "src1": "demographic"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables hash_token --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``demographic`` table (or the table named in ``src1``) must
          be processed and registered as a Spark temp view before
          ``hash_token``.
        - The ``pipeline`` value must be present in the context dict (used
          for the ``SOURCE`` column in stub rows).
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, current_timestamp, lit
from pyspark.sql.types import StringType

from ovid2.transformations.fixers.base import BaseFixer


class AddMissingHashTokenPatidsFixer(BaseFixer):
    """Add stub hash_token rows for PATIDs that exist in demographic but not hash_token.

    Performs a left-anti join to find PATIDs in the demographic table that are
    not present in the hash_token DataFrame. For each missing PATID, creates
    a stub row with null token columns (derived from the existing DataFrame
    schema), null TOKEN_ENCRYPTION_KEY, a current timestamp for UPDATED, and
    the pipeline name for SOURCE. The stubs are unioned with the existing
    hash_token DataFrame.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "add_missing_hash_token_patids"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Add stub rows for PATIDs missing from the hash_token table.

        Args:
            df: The hash_token DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Demographic table name to read PATIDs from
                  (e.g., ``"demographic"``).

        Returns:
            DataFrame with stub rows added for missing PATIDs.
        """
        demographic_table: str = context["src1"]
        pipeline: str = context["pipeline"]

        demographic_df: DataFrame = context["spark"].table(demographic_table)

        # Find PATIDs in demographic not in the current hash_token df
        demographic_patids: DataFrame = demographic_df.select(
            col("PATID").alias("_demo_patid"),
        ).distinct()

        hash_token_patids: DataFrame = df.select(
            col("PATID").alias("_ht_patid"),
        ).distinct()

        # Broadcast the hash_token PATID set — it is at most the size of
        # the demographic PATID set, which already fits in an executor
        # broadcast in any realistic deployment.
        missing_patids: DataFrame = demographic_patids.join(
            broadcast(hash_token_patids),
            on=(col("_demo_patid") == col("_ht_patid")),
            how="left_anti",
        ).select(col("_demo_patid").alias("PATID"))

        # Build stub rows: PATID + null for all other columns from the schema
        existing_columns: list[str] = df.columns
        stub_rows: DataFrame = missing_patids

        for column_name in existing_columns:
            if column_name == "PATID":
                continue
            if column_name == "UPDATED":
                stub_rows = stub_rows.withColumn(
                    "UPDATED",
                    current_timestamp().cast(StringType()),
                )
            elif column_name == "SOURCE":
                stub_rows = stub_rows.withColumn(
                    "SOURCE",
                    lit(pipeline.upper()),
                )
            else:
                # All token columns and TOKEN_ENCRYPTION_KEY get null
                stub_rows = stub_rows.withColumn(
                    column_name,
                    lit(None).cast(StringType()),
                )

        # Ensure column order matches the existing hash_token table
        stub_rows = stub_rows.select(existing_columns)

        # Union stubs with existing hash_token
        result: DataFrame = df.unionByName(stub_rows)

        return result
