"""Fix: Remove rows where the specified field is null.

Summary:
    Functionally identical to ``delete_when_null`` -- filters out rows where
    the specified field is null.  The name ``invalid_to_null`` is carried
    over from the original codebase for backward compatibility with existing
    pipeline fixer configs.  In the original implementation, this fix
    converted "invalid" values to null and then removed them; in the
    refactored version the mapper already handles value normalization, so
    only the null-removal step remains.  New configurations should prefer
    ``delete_when_null`` for clarity.

Use Cases:
    - Remove encounter rows where ENCOUNTERID is null (legacy config name
      preserved from the original codebase).
      Config::

          {"fix": "invalid_to_null", "src1": "ENCOUNTERID"}

    - Equivalent to ``delete_when_null`` -- use either name.
      Config::

          {"fix": "invalid_to_null", "src1": "DX"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Order is flexible -- this
       fix operates on the current DataFrame only (no cross-table joins).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "encounter": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "invalid_to_null", "src1": "ENCOUNTERID"},
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
        - The column named in ``src1`` must exist in the DataFrame.
        - No cross-table dependencies.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col

from ovid2.transformations.fixers.base import BaseFixer


class InvalidToNullFixer(BaseFixer):
    """Remove rows where the specified field is null.

    Despite the name (carried over from the original codebase), this fixer
    filters out rows where ``context['src1']`` is null — effectively the
    same as a ``delete_when_null`` operation.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "invalid_to_null"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows where the specified field is null.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Field name to check for null values.

        Returns:
            DataFrame with null-field rows removed.
        """
        field_name: str = context["src1"]

        return df.filter(col(field_name).isNotNull())
