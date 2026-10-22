"""Fix: Remove rows whose column value appears in a reference list file.

Summary:
    Filters out rows from the DataFrame where a specified column value
    matches any entry in a reference list file.  The list file is a
    single-column CSV stored in ``material/lists/`` (e.g., LOINC codes to
    exclude, SNOMED PII codes to redact).  Uses a Spark ``left_anti`` join
    for efficient, fully lazy filtering -- neither the main DataFrame nor
    the reference list is collected to the driver.  Commonly used for
    removing known bad codes, PII-related codes, or codes that should not
    appear in the final output.

Use Cases:
    - Remove lab results with LOINC codes that appear in the exclusion
      list (e.g., panel-only or deprecated LOINC codes).
      Config::

          {"fix": "list_remover", "src1": "loinc_list", "src2": "LAB_LOINC"}

    - Remove obs_clin/obs_gen rows with SNOMED PII codes (e.g., Social
      Security Number observations that must be redacted).
      Config::

          {"fix": "list_remover", "src1": "snomed_pii_list", "src2": "OBSCLIN_CODE"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Typically placed after
       orphan fixes and replication.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "lab_result_cm": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "list_remover", "src1": "loinc_list", "src2": "LAB_LOINC"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables lab_result_cm --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The reference list file must exist at
          ``material/lists/{src1}.csv`` (e.g.,
          ``material/lists/loinc_list.csv``).
        - The column named in ``src2`` must exist in the DataFrame.
"""

import os
from typing import Any

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import broadcast, col

from ovid2.config.settings import COMMON_LISTS_DIR
from ovid2.transformations.fixers.base import BaseFixer


class ListRemoverFixer(BaseFixer):
    """Remove rows whose column value is present in a reference list.

    Reads a list file from ``material/lists/{src1}.csv`` as a single-column
    CSV, then performs a ``left_anti`` join to remove matching rows from the
    input DataFrame. The column to check is specified by ``context['src2']``.

    The list file is read via Spark as a lazy DataFrame — no ``.collect()``
    is used. The anti-join is fully lazy.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "list_remover"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows whose column value is in the reference list.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``spark``: SparkSession instance.
                - ``src1``: List file name (without ``.csv`` extension and
                  without path prefix). Resolved to
                  ``material/lists/{src1}.csv`` via ``COMMON_LISTS_DIR``.
                - ``src2``: Column name in ``df`` to check against the list.

        Returns:
            DataFrame with matching rows removed.
        """
        spark: SparkSession = context["spark"]
        list_name: str = context["src1"]
        column_name: str = context["src2"]

        list_path: str = os.path.join(COMMON_LISTS_DIR, f"{list_name}.csv")

        list_df: DataFrame = spark.read.load(
            list_path,
            format="csv",
            inferSchema="false",
            header="false",
            quote='"',
        )

        # The single-column CSV is read as _c0 — alias it for the join
        list_values: DataFrame = list_df.select(
            col("_c0").alias("_list_value"),
        )

        # Left-anti join: keep rows whose column value is NOT in the list.
        # The reference list is small (at most tens of thousands of rows) and
        # fits easily in an executor-side broadcast, eliminating the need to
        # shuffle the main DataFrame by the join key.
        result: DataFrame = df.join(
            broadcast(list_values),
            on=(col(column_name) == col("_list_value")),
            how="left_anti",
        )

        return result
