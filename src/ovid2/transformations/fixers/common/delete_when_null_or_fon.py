"""Fix: Delete rows where a specified field is null, empty, or a flavor of null.

Summary:
    A stricter variant of ``delete_when_null`` that also removes rows where
    the field is empty, whitespace-only, or contains a PCORnet
    flavor-of-null sentinel value (``NI``, ``UN``, ``OT``).  These sentinel
    values indicate that the source system explicitly reported "no
    information", "unknown", or "other" -- which for certain required
    fields is equivalent to having no usable data.  Use this when the
    field must contain a real, meaningful value to be valid.

Use Cases:
    - Remove obs_clin rows where ``OBSCLIN_CODE`` is null, empty, or a
      flavor of null -- an observation without a code is not usable.
      Config::

          {"fix": "delete_when_null_or_fon", "src1": "OBSCLIN_CODE"}

    - Remove procedures rows where ``PX`` is effectively missing, catching
      cases where the mapper produced ``NI`` instead of a real code.
      Config::

          {"fix": "delete_when_null_or_fon", "src1": "PX"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Order is flexible -- this
       fix operates on the current DataFrame only (no cross-table joins).
       Typically placed after orphan and replication fixes.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "obs_clin": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "delete_when_null_or_fon", "src1": "OBSCLIN_CODE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260101 \\
               --tables obs_clin --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260101 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The column named in ``src1`` must exist in the DataFrame.
        - No cross-table dependencies.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, trim

from ovid2.transformations.fixers.base import BaseFixer


class DeleteWhenNullOrFonFixer(BaseFixer):
    """Remove rows where a specified field is null, empty, or a flavor of null.

    Filters out any row where the column named in ``context['src1']`` is:
    - Null
    - Empty or whitespace-only
    - A PCORnet flavor-of-null sentinel value: ``NI``, ``UN``, or ``OT``

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "delete_when_null_or_fon"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Filter out rows where the field is null, empty, or a flavor of null.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Field name to check.

        Returns:
            DataFrame with null / empty / flavor-of-null rows removed.
        """
        field_name: str = context["src1"]

        is_invalid = (
            col(field_name).isNull()
            | (trim(col(field_name)) == "")
            | col(field_name).isin("NI", "UN", "OT")
        )

        return df.filter(~is_invalid)
