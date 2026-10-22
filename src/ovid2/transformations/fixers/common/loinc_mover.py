"""Fix: Remove misplaced LOINC rows from the current table based on classtype.

Summary:
    Enforces correct LOINC code placement across the three LOINC-bearing
    PCORnet tables (``lab_result_cm``, ``obs_clin``, ``obs_gen``).  Some
    LOINC codes belong exclusively to one table based on their classtype or
    ``found_in`` designation in the LOINC location reference.  This fix
    removes rows whose LOINC code belongs in a different table, preventing
    duplicate or misplaced results.  It is configured as a **multi-table
    fix** in ``MULTI_TABLE_FIXES`` (not in per-table ``FIXER_CHAINS``)
    because it must run consistently across all three tables after per-table
    fixes complete.

    Version 1 (classtype-based): Uses the ``classtype`` column from the
    LOINC location reference to determine placement:
        - classtype=1 -> lab_result_cm
        - classtype=2 -> obs_clin
        - classtype=3,4 -> obs_gen

    Version 2 (found_in-based): Uses ``found_in_lab``, ``found_in_obs_clin``,
    ``found_in_obs_gen`` columns to determine correct placement.

    For each table, the fix keeps only rows whose LOINC codes belong there
    (or rows with no LOINC match in the reference, which are kept as-is).

Use Cases:
    - Version 2 (found_in-based): most pipelines use v2 which checks the
      ``found_in_lab``, ``found_in_obs_clin``, ``found_in_obs_gen`` columns
      in the LOINC location reference.
      Config (in ``MULTI_TABLE_FIXES``)::

          {"fix": "loinc_mover", "version": "v2"}

    - Version 1 (classtype-based): legacy mode using classtype 1/2/3/4
      from the LOINC location reference.
      Config::

          {"fix": "loinc_mover", "version": "v1"}

Deployment:
    1. Add the fix dict to ``MULTI_TABLE_FIXES`` (**not** ``FIXER_CHAINS``)
       in ``pipelines/<pipeline>/fixer_configs.py``::

           from typing import Any

           MULTI_TABLE_FIXES: list[dict[str, Any]] = [
               {"fix": "loinc_mover", "version": "v2"},
           ]

    2. Run a full pipeline that includes ``lab_result_cm``, ``obs_clin``,
       and ``obs_gen`` tables with ``fix`` in transformations::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables lab_result_cm obs_clin obs_gen \\
               --transformations all \\
               --input_address disc_input --output_address disc_output

    3. Or include all tables in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The LOINC location reference file must exist at the path
          configured in ``core/config/settings.py``
          (``COMMON_LISTS_DIR / lab_loinc_location.csv``).
        - All three tables (``lab_result_cm``, ``obs_clin``, ``obs_gen``)
          should be included in the pipeline run for consistent results.
        - Multi-table fixes run **after** all per-table ``FIXER_CHAINS``
          have completed.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col, lit

from ovid2.transformations.fixers.base import BaseFixer

# Maps table names to the classtype values that belong in that table (v1).
_V1_CLASSTYPE_MAP: dict[str, list[str]] = {
    "lab_result_cm": ["1"],
    "obs_clin": ["2"],
    "obs_gen": ["3", "4"],
}

# Maps table names to the LOINC code column used for the join.
_TABLE_LOINC_COLUMN: dict[str, str] = {
    "lab_result_cm": "LAB_LOINC",
    "obs_clin": "OBSCLIN_CODE",
    "obs_gen": "OBSGEN_CODE",
}


class LoincMoverFixer(BaseFixer):
    """Remove rows whose LOINC code belongs in a different CDM table.

    Joins the current table against a LOINC location reference DataFrame to
    identify misplaced rows. Rows whose LOINC classtype does not match the
    current table are removed. Rows with no LOINC match in the reference
    (unrecognized codes) are retained.

    In version 1, classtype determines placement:
        - lab_result_cm keeps classtype=1 or no-match rows
        - obs_clin keeps classtype=2 or no-match rows
        - obs_gen keeps classtype=3,4 or no-match rows

    In version 2, ``found_in_lab``, ``found_in_obs_clin``, and
    ``found_in_obs_gen`` columns determine placement.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "loinc_mover"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Remove rows whose LOINC classtype does not match the current table.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Version string (``"1"`` or ``"2"``).
                - ``loinc_location_df``: Pre-loaded LOINC location reference
                  DataFrame with ``loinc_num`` and classtype/found_in columns.
                - ``table_name``: Name of the current table being processed
                  (e.g., ``"lab_result_cm"``, ``"obs_clin"``, ``"obs_gen"``).

        Returns:
            DataFrame with misplaced LOINC rows removed.

        Raises:
            ValueError: If the table name is not supported for LOINC moving
                or if the version is not recognized.
        """
        version: str = context["src1"]
        loinc_ref: DataFrame = context["loinc_location_df"]
        table_name: str = context["table_name"]

        loinc_column: str | None = _TABLE_LOINC_COLUMN.get(table_name)
        if loinc_column is None:
            raise ValueError(
                f"Table '{table_name}' is not supported by the loinc_mover fix. "
                f"Supported tables: {list(_TABLE_LOINC_COLUMN.keys())}"
            )

        # Normalize version strings — accept "1", "1.0", "v1", "2", "2.0", "v2"
        version_clean = version.lower().lstrip("v").split(".")[0]

        if version_clean == "1":
            return self._apply_v1(df, loinc_ref, table_name, loinc_column)

        if version_clean == "2":
            return self._apply_v2(df, loinc_ref, table_name, loinc_column)

        raise ValueError(
            f"Unrecognized loinc_mover version '{version}'. "
            "Supported versions: '1', '2' (also accepts '1.0', '2.0', 'v1', 'v2')."
        )

    def _apply_v1(
        self,
        df: DataFrame,
        loinc_ref: DataFrame,
        table_name: str,
        loinc_column: str,
    ) -> DataFrame:
        """Apply version 1 logic: filter by classtype.

        Rows whose LOINC code matches a classtype that does NOT belong in the
        current table are removed. Rows with no reference match are kept.

        Args:
            df: The DataFrame to fix.
            loinc_ref: LOINC location reference with ``loinc_num`` and
                ``classtype`` columns.
            table_name: Current table name.
            loinc_column: Column name in ``df`` containing the LOINC code.

        Returns:
            DataFrame with misplaced LOINC rows removed.
        """
        valid_classtypes: list[str] = _V1_CLASSTYPE_MAP[table_name]

        # Select only needed columns and alias to avoid ambiguity
        ref_slim: DataFrame = loinc_ref.select(
            col("loinc_num").alias("_loinc_ref_num"),
            col("classtype").alias("_loinc_classtype"),
        )

        # Broadcast the LOINC reference — it is small lookup data
        joined: DataFrame = df.join(
            broadcast(ref_slim),
            on=(col(loinc_column) == col("_loinc_ref_num")),
            how="left",
        )

        # Keep rows where: no match (unrecognized LOINC) OR classtype is valid
        result: DataFrame = joined.filter(
            col("_loinc_classtype").isNull() | col("_loinc_classtype").isin(valid_classtypes)
        ).drop("_loinc_ref_num", "_loinc_classtype")

        return result

    def _apply_v2(
        self,
        df: DataFrame,
        loinc_ref: DataFrame,
        table_name: str,
        loinc_column: str,
    ) -> DataFrame:
        """Apply version 2 logic: filter by found_in columns.

        Version 2 uses ``found_in_lab``, ``found_in_obs_clin``, and
        ``found_in_obs_gen`` columns from the LOINC reference to determine
        where each LOINC code belongs.

        Args:
            df: The DataFrame to fix.
            loinc_ref: LOINC location reference with ``loinc_num`` and
                ``found_in_lab``, ``found_in_obs_clin``, ``found_in_obs_gen``
                columns.
            table_name: Current table name.
            loinc_column: Column name in ``df`` containing the LOINC code.

        Returns:
            DataFrame with misplaced LOINC rows removed.
        """
        # Map table names to the found_in column that indicates membership
        found_in_column_map: dict[str, str] = {
            "lab_result_cm": "found_in_lab",
            "obs_clin": "found_in_obs_clin",
            "obs_gen": "found_in_obs_gen",
        }

        found_in_col: str = found_in_column_map[table_name]

        ref_slim: DataFrame = loinc_ref.select(
            col("loinc_num").alias("_loinc_ref_num"),
            col(found_in_col).alias("_loinc_found_in"),
        )

        # Broadcast the LOINC reference — it is small lookup data
        joined: DataFrame = df.join(
            broadcast(ref_slim),
            on=(col(loinc_column) == col("_loinc_ref_num")),
            how="left",
        )

        # Keep rows where: no match (unrecognized LOINC) OR found_in == '1'
        result: DataFrame = joined.filter(
            col("_loinc_found_in").isNull() | (col("_loinc_found_in") == lit("1"))
        ).drop("_loinc_ref_num", "_loinc_found_in")

        return result
