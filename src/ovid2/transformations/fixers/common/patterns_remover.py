"""Fix: Remove rows where a column matches any pattern from a patterns list.

Summary:
    Filters out rows where a specified column value matches any regex
    pattern defined in a reference patterns file.  The patterns file is a
    CSV stored in ``material/lists/`` containing text patterns (e.g.,
    partial strings, keywords) that indicate PII or invalid data.  The
    fix reads the small patterns file, builds a combined case-insensitive
    regex, and applies ``rlike()`` filtering on the main DataFrame.  Rows
    with null values in the target column are preserved (not filtered out).
    The patterns file is ``.collect()``-ed to build the regex -- acceptable
    because pattern lists are small reference data (typically < 100
    entries).  The actual filtering of the main DataFrame is fully lazy.

Use Cases:
    - Remove lab results whose ``RAW_RESULT`` matches PII patterns (e.g.,
      patient names, addresses, SSN fragments embedded in free-text result
      fields).
      Config::

          {"fix": "patterns_remover", "src1": "patterns_list", "src2": "RAW_RESULT"}

    - Remove obs_clin rows whose ``OBSCLIN_RESULT_TEXT`` contains
      identifiable information matching the patterns list.
      Config::

          {"fix": "patterns_remover", "src1": "patterns_list", "src2": "OBSCLIN_RESULT_TEXT"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Typically placed after
       orphan fixes and replication, often alongside ``list_remover``.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "lab_result_cm": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "list_remover", "src1": "loinc_list", "src2": "LAB_LOINC"},
                   {"fix": "patterns_remover", "src1": "patterns_list", "src2": "RAW_RESULT"},
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
        - The patterns file must exist at
          ``material/lists/{src1}.csv`` (e.g.,
          ``material/lists/patterns_list.csv``).  The file must have a
          header row.
        - The column named in ``src2`` must exist in the DataFrame.
"""

import os
from typing import Any

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import col

from ovid2.config.settings import COMMON_LISTS_DIR
from ovid2.transformations.fixers.base import BaseFixer


class PatternsRemoverFixer(BaseFixer):
    """Remove rows where a column matches any pattern from a reference list.

    Reads a patterns CSV file, constructs a combined ``rlike()`` regex, and
    filters out rows where the specified column matches. Null values in the
    target column are preserved (not filtered out).

    The patterns file is read lazily, but its values must be collected to
    build the regex string. This is acceptable because pattern lists are
    small reference data (typically < 100 entries). The actual filtering
    of the main DataFrame is fully lazy.

    All operations on the main DataFrame are lazy Spark transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "patterns_remover"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows where the column matches any pattern in the list.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``spark``: SparkSession instance.
                - ``src1``: Patterns list name (resolved to
                  ``material/lists/{src1}.csv`` via ``COMMON_LISTS_DIR``).
                - ``src2``: Column name in ``df`` to check against patterns.

        Returns:
            DataFrame with pattern-matching rows removed.
        """
        spark: SparkSession = context["spark"]
        patterns_name: str = context["src1"]
        column_name: str = context["src2"]

        patterns_path: str = os.path.join(
            COMMON_LISTS_DIR,
            f"{patterns_name}.csv",
        )

        patterns_df: DataFrame = spark.read.load(
            patterns_path,
            format="csv",
            inferSchema="false",
            header="true",
            quote='"',
        )

        # Collect patterns to build regex — patterns lists are small reference
        # data, so this single collect is acceptable. TODO(P4): read patterns
        # via a broadcast join to avoid driver-side materialisation entirely.
        pattern_rows: list = patterns_df.collect()  # noqa: OVID2001 small reference data
        patterns: list[str] = [str(row[0]) for row in pattern_rows if row[0] is not None]

        if not patterns:
            return df

        # Build case-insensitive regex: (?i).*pattern1.*|.*pattern2.*|...
        regex_parts: list[str] = [f".*{pattern}.*" for pattern in patterns]
        regex_pattern: str = f"(?i){'|'.join(regex_parts)}"

        # Keep rows where column is null OR does NOT match the regex
        result: DataFrame = df.filter(
            col(column_name).isNull() | ~col(column_name).rlike(regex_pattern)
        )

        return result
