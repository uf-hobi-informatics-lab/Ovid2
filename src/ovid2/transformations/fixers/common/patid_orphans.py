"""Fix: Remove rows whose PATID does not exist in a reference table.

Summary:
    Ensures referential integrity by removing rows whose patient ID does not
    appear in a reference table (typically ``demographic``).  Orphan PATIDs
    indicate records for patients not included in the demographic extract,
    making them unlinkable and invalid for analysis.  This is the most
    fundamental data quality fix and is applied to nearly every clinical
    table in every pipeline's fix chain.

Use Cases:
    - Standard orphan removal on the encounter table: removes encounters
      for patients not present in the demographic table.
      Config::

          {"fix": "patid_orphans", "src1": "demographic"}

    - Pat_relationship table with a non-default ID column: the table uses
      ``PATID_1`` and ``PATID_2`` instead of ``PATID``, so ``src2``
      specifies which column to check.  Run the fix twice, once per column.
      Config::

          {"fix": "patid_orphans", "src1": "demographic", "src2": "PATID_1"}
          {"fix": "patid_orphans", "src1": "demographic", "src2": "PATID_2"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place ``patid_orphans`` as
       the **first** fix in the chain -- downstream fixes assume all PATIDs
       are valid.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   # ... other fixes after ...
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables encounter --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run (all tables, all stages)::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The reference table (``src1``, usually ``demographic``) must be
          processed and registered as a Spark temp view **before** the
          target table.  The default table ordering in ``tables_keys.py``
          ensures ``demographic`` is processed first.
        - When using ``src2``, the column name must exist in the input
          DataFrame (e.g., ``PATID_1`` for ``pat_relationship``).
"""

import logging
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col

from ovid2.transformations.fixers.base import BaseFixer

logger = logging.getLogger(__name__)


class PatidOrphansFixer(BaseFixer):
    """Remove rows whose PATID is not present in a reference table.

    Performs a left semi join against the distinct PATIDs from the reference
    table specified in ``context['src1']``. Rows in the input DataFrame
    whose PATID has no match in the reference are dropped.

    When ``context['src2']`` is provided, it specifies the column name in
    the input DataFrame to check (e.g., ``"PATID_1"`` or ``"PATID_2"`` for
    the ``pat_relationship`` table). The reference table always uses
    ``"PATID"``.  When ``src2`` is absent or a sentinel value like
    ``"NotUsed"``, the default column ``"PATID"`` is used.

    All operations are lazy Spark DataFrame transformations.
    """

    # Sentinel values that some pipeline configs use to mean "not applicable"
    _SENTINEL_VALUES = {"", "NotUsed", "notused", "NA", "na", "N/A"}

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "patid_orphans"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows whose PATID does not exist in the reference table.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Reference table name to read PATIDs from
                  (e.g., ``"demographic"``).
                - ``src2`` (optional): Column name in ``df`` containing the
                  patient ID to check.  Defaults to ``"PATID"`` when absent
                  or set to a sentinel value.

        Returns:
            DataFrame with orphan PATID rows removed.
        """
        reference_table: str = context["src1"]

        # Determine which column in the input DF holds the patient ID
        raw_src2: str = context.get("src2", "")
        patid_column: str = (
            raw_src2 if raw_src2 and raw_src2 not in self._SENTINEL_VALUES else "PATID"
        )

        reference_df: DataFrame = context["spark"].table(reference_table)

        valid_patids: DataFrame = reference_df.select(
            col("PATID").alias("_valid_patid"),
        ).distinct()

        logger.debug(
            "patid_orphans: checking column '%s' against '%s'.PATID",
            patid_column,
            reference_table,
        )

        return df.join(
            broadcast(valid_patids),
            on=(col(patid_column) == col("_valid_patid")),
            how="left_semi",
        ).drop("_valid_patid")
