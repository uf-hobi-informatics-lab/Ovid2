"""Fix: Remove rows with panel-type LOINC codes (DC_1_20 remediation).

Summary:
    Removes rows containing panel-order LOINC codes, which represent orders
    for groups of tests (e.g., "CBC panel", "BMP panel") rather than
    individual results.  The PCORnet EDC DC_1_20 check flags tables where
    panel LOINCs exceed a threshold percentage of all LOINC-coded records.
    This fix splits the DataFrame into LOINC-coded rows
    (``TYPE_COLUMN == 'LC'``) and non-LOINC rows.  Among the LOINC rows,
    those matching panel LOINCs from a pre-loaded reference are removed via
    broadcast ``left_anti`` join.  Non-LOINC rows pass through
    unconditionally.  If the panel reference DataFrame is not provided, the
    fix passes through unchanged.

Use Cases:
    - Remove panel LOINCs from the obs_clin table to satisfy DC_1_20.
      Config::

          {"fix": "panel_loinc_remover", "src1": "sf_server", "src2": "OBSCLIN_CODE,OBSCLIN_TYPE"}

    - Remove panel LOINCs from lab_result_cm (the code and type column
      happen to share the same base name for this table).
      Config::

          {"fix": "panel_loinc_remover", "src1": "sf_server", "src2": "LAB_LOINC,LAB_LOINC"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  The ``src2`` is a
       comma-separated pair: ``"CODE_COLUMN,TYPE_COLUMN"``.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "obs_clin": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "panel_loinc_remover", "src1": "sf_server", "src2": "OBSCLIN_CODE,OBSCLIN_TYPE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables obs_clin --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The panel LOINC reference DataFrame (``panel_loinc_df``) must be
          pre-loaded and provided in the context by the pipeline
          orchestrator.  If not available, the fix silently passes through.
        - The columns named in ``src2`` (code column and type column) must
          exist in the DataFrame.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, upper

from ovid2.transformations.fixers.base import BaseFixer

# The type value that indicates LOINC-coded rows in PCORnet CDM.
_LOINC_TYPE_VALUE: str = "LC"


class PanelLoincRemoverFixer(BaseFixer):
    """Remove rows with panel-type LOINC codes from the DataFrame.

    Splits the DataFrame into LOINC-coded rows and non-LOINC rows based on
    the type column. Among LOINC rows, those whose code matches a panel LOINC
    in the reference are removed via a ``left_anti`` join. The reference panel
    LOINCs should be provided in ``context['panel_loinc_df']`` as a pre-loaded
    DataFrame. If not available, the fix passes through unchanged.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "panel_loinc_remover"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows with panel-type LOINC codes.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Snowflake DB server name (used when reading from
                  Snowflake directly; informational in lazy architecture).
                - ``src2``: Comma-separated ``"CODE_COLUMN,TYPE_COLUMN"``
                  identifying the LOINC code column and the type column
                  (e.g., ``"OBSCLIN_CODE,OBSCLIN_TYPE"`` for obs_clin,
                  ``"LAB_LOINC,LAB_LOINC"`` for lab_result_cm where both
                  code and type use the same conceptual column).
                - ``panel_loinc_df`` (optional): Pre-loaded DataFrame of
                  panel LOINCs with a ``LOINC_NUM`` and ``PANEL_TYPE``
                  column. If not provided, the fix returns ``df`` unchanged.

        Returns:
            DataFrame with panel LOINC rows removed.

        Raises:
            ValueError: If ``src2`` does not contain exactly two
                comma-separated column names.
        """
        src2_raw: str = context["src2"]
        src2_parts: list[str] = [part.strip() for part in src2_raw.split(",")]
        if len(src2_parts) != 2:
            raise ValueError(f"Expected 'CODE_COLUMN,TYPE_COLUMN' in src2, got: '{src2_raw}'")

        code_column: str = src2_parts[0]
        type_column: str = src2_parts[1]

        panel_loinc_df: DataFrame | None = context.get("panel_loinc_df")
        if panel_loinc_df is None:
            # No panel LOINC reference available — pass through unchanged
            return df

        # Extract panel-type LOINC codes from the reference
        panel_loincs: DataFrame = (
            panel_loinc_df.filter(upper(col("PANEL_TYPE")) == "PANEL")
            .select(col("LOINC_NUM").alias("_panel_loinc_num"))
            .distinct()
        )

        # Broadcast the panel list — it is small reference data
        panel_loincs_bc: DataFrame = broadcast(panel_loincs)

        # Split: only evaluate LOINC-coded rows for panel removal
        loinc_rows: DataFrame = df.filter(upper(col(type_column)) == _LOINC_TYPE_VALUE)
        non_loinc_rows: DataFrame = df.filter(
            col(type_column).isNull() | (upper(col(type_column)) != _LOINC_TYPE_VALUE)
        )

        # Among LOINC rows, keep only non-panel ones
        non_panel_loinc_rows: DataFrame = loinc_rows.join(
            panel_loincs_bc,
            on=(col(code_column) == col("_panel_loinc_num")),
            how="left_anti",
        )

        # Combine non-LOINC rows + non-panel LOINC rows
        result: DataFrame = non_loinc_rows.unionByName(non_panel_loinc_rows)

        return result
