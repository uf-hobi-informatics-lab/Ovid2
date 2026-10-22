"""Fix: Delete rows where a specified field is null.

Summary:
    Removes rows where a required field contains a null value.  This is
    one of the simplest data quality fixes -- it enforces a "not null"
    constraint on a specified column.  Commonly used to remove records
    that are missing a primary identifier or key clinical value (e.g.,
    ``DX`` for diagnosis, ``PX`` for procedures, ``ADMIT_DATE`` for
    encounters).

Use Cases:
    - Remove encounter rows that have no admit date, since encounters
      without dates cannot be placed on a timeline.
      Config::

          {"fix": "delete_when_null", "src1": "ADMIT_DATE"}

    - Remove diagnosis rows with no diagnosis code, since a diagnosis
      record without a code has no clinical meaning.
      Config::

          {"fix": "delete_when_null", "src1": "DX"}

    - Remove procedures rows with no procedure code.
      Config::

          {"fix": "delete_when_null", "src1": "PX"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Order is flexible -- this
       fix operates on the current DataFrame only (no cross-table joins).
       Typically placed after orphan and replication fixes.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "delete_when_null", "src1": "ADMIT_DATE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables encounter --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The column named in ``src1`` must exist in the DataFrame.
        - No cross-table dependencies.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col

from ovid2.transformations.fixers.base import BaseFixer


class DeleteWhenNullFixer(BaseFixer):
    """Remove rows where a specified field is null.

    Filters out any row where the column named in ``context['src1']``
    contains a null value.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "delete_when_null"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Filter out rows where the specified field is null.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Field name to check for null values.

        Returns:
            DataFrame with null-valued rows removed.
        """
        field_name: str = context["src1"]

        return df.filter(col(field_name).isNotNull())
