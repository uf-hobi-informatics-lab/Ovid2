"""Fix: Copy a source field value into a target field when the target is null.

Summary:
    Backfills missing values in one column using values from another column
    in the same row.  This is a simple coalesce operation -- if the target
    field is null, it takes the source field's value; otherwise it keeps
    the original.  No rows are added or deleted.  Commonly used to populate
    derived date fields from related source dates within the same record,
    or to fill a preferred field from a fallback field.

Use Cases:
    - Backfill ``ONSET_DATE`` from ``REPORT_DATE`` in the condition table
      when the onset date was not recorded by the source system.
      Config::

          {"fix": "copy_when_null", "src1": "ONSET_DATE", "src2": "REPORT_DATE"}

    - Backfill ``MEASURE_DATE`` from ``OBSCLIN_START_DATE`` in obs_clin
      when the measure date is missing.
      Config::

          {"fix": "copy_when_null", "src1": "MEASURE_DATE", "src2": "OBSCLIN_START_DATE"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Order is flexible -- this
       fix operates on the current row only (no cross-table joins), so it
       can appear anywhere in the chain.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "condition": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "copy_when_null", "src1": "ONSET_DATE", "src2": "REPORT_DATE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables condition --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - Both ``src1`` (target) and ``src2`` (source) columns must exist
          in the DataFrame.
        - No cross-table dependencies.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, when

from ovid2.transformations.fixers.base import BaseFixer


class CopyWhenNullFixer(BaseFixer):
    """Copy a source column value into the target column when the target is null.

    For each row, if the target field (``src1``) is null, it is replaced with
    the value from the source field (``src2``).  Non-null target values are
    left unchanged.  No rows are added or removed.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "copy_when_null"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Copy the source field into the target field where the target is null.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Target field name (destination).
                - ``src2``: Source field name to copy from.

        Returns:
            DataFrame with null target values filled from the source field.
        """
        target_field: str = context["src1"]
        source_field: str = context["src2"]

        result: DataFrame = df.withColumn(
            target_field,
            when(col(target_field).isNull(), col(source_field)).otherwise(col(target_field)),
        )

        return result
