"""Fix: Normalize NORM_RANGE and NORM_MODIFIER fields for DC_3_10 compliance.

Summary:
    Normalizes the normal range fields in ``lab_result_cm`` to satisfy the
    PCORnet EDC DC_3_10 "fully specified normal range" requirement.  Many
    source systems provide inconsistent or incomplete normal range data
    (e.g., both modifiers set to ``NI`` when ranges are numeric, or ``EQ/EQ``
    when only one bound is populated).  This fix applies four sequential
    rules that coerce the fields into one of three valid DC_3_10 patterns:
    (1) both-bounds (EQ/EQ with both ranges), (2) lower-only (GE or GT with
    MOD_HIGH=NO), or (3) upper-only (LE or LT with MOD_LOW=NO).  No rows
    are added or deleted -- only the four NORM fields are modified.  Applied
    exclusively to the ``lab_result_cm`` table.

    DC_3_10 requires one of three valid patterns:
        1. Both bounds:  MOD_LOW=EQ, MOD_HIGH=EQ, RANGE_LOW not null, RANGE_HIGH not null
        2. Lower-only:   MOD_LOW in (GT,GE), MOD_HIGH=NO, RANGE_LOW not null, RANGE_HIGH is null
        3. Upper-only:   MOD_HIGH in (LT,LE), MOD_LOW=NO, RANGE_HIGH not null, RANGE_LOW is null

Use Cases:
    - Normalize normal ranges in the lab_result_cm table.  No parameters
      are needed -- the fix operates on the fixed column names
      ``NORM_RANGE_LOW``, ``NORM_RANGE_HIGH``, ``NORM_MODIFIER_LOW``, and
      ``NORM_MODIFIER_HIGH``.
      Config::

          {"fix": "norm_range_normalization"}

Deployment:
    1. Add the fix dict to the ``lab_result_cm`` table's entry in
       ``FIXER_CHAINS`` in ``pipelines/<pipeline>/fixer_configs.py``.
       Typically placed as one of the last fixes for lab_result_cm
       (after orphan, replication, and list/pattern removal fixes).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "lab_result_cm": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "list_remover", "src1": "loinc_list", "src2": "LAB_LOINC"},
                   {"fix": "norm_range_normalization"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables lab_result_cm --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The DataFrame must contain the four NORM columns:
          ``NORM_RANGE_LOW``, ``NORM_RANGE_HIGH``, ``NORM_MODIFIER_LOW``,
          and ``NORM_MODIFIER_HIGH``.
        - No cross-table dependencies.  No ``src1``/``src2`` parameters
          needed.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql.column import Column

from ovid2.transformations.fixers.base import BaseFixer


def _is_numeric_value(column: Column) -> Column:
    """Return a boolean Column that is True when the value is a parseable number.

    Uses ``cast('double')`` to detect numeric strings, matching the semantics
    of SQL Server's ``ISNUMERIC()``. Null, empty, and non-numeric strings
    return False.

    Args:
        column: The PySpark Column to test.

    Returns:
        A boolean Column expression.
    """
    return F.trim(column).cast("double").isNotNull()


def _is_null_or_empty_or_ni(column: Column) -> Column:
    """Return a boolean Column that is True when the value is null, empty, or NI.

    Treats null, empty/whitespace-only strings, and the PCORnet sentinel
    ``NI`` (No Information) as equivalent — all indicate no value present.

    Args:
        column: The PySpark Column to test.

    Returns:
        A boolean Column expression.
    """
    return column.isNull() | (F.trim(column) == "") | (F.upper(F.trim(column)) == "NI")


class NormRangeNormalizationFixer(BaseFixer):
    """Normalize NORM_RANGE and NORM_MODIFIER fields in lab_result_cm.

    Applies four sequential rules using chained ``withColumn`` expressions:

    **Rule 1**: Both NORM_RANGE_LOW and HIGH are numeric + both modifiers are
    NI -> set both modifiers to EQ.

    **Rule 2a**: Both modifiers EQ + only LOW populated -> set MOD_LOW=GE,
    MOD_HIGH=NO, RANGE_HIGH=null.

    **Rule 2b**: Both modifiers EQ + only HIGH populated -> set MOD_LOW=NO,
    MOD_HIGH=LE, RANGE_LOW=null.

    **Rule 3**: MOD_HIGH in (LT,LE) + RANGE_LOW is NI/null/empty -> set
    RANGE_LOW=null, MOD_LOW=NO.

    **Rule 4**: MOD_LOW in (GT,GE) + RANGE_HIGH is NI/null/empty -> set
    RANGE_HIGH=null, MOD_HIGH=NO.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "norm_range_normalization"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Apply norm range normalization rules.

        Args:
            df: The lab_result_cm DataFrame to fix. Must contain columns
                ``NORM_RANGE_LOW``, ``NORM_RANGE_HIGH``, ``NORM_MODIFIER_LOW``,
                and ``NORM_MODIFIER_HIGH``.
            context: Dictionary containing standard context keys. No
                fix-specific parameters (``src1``/``src2``) are needed.

        Returns:
            DataFrame with NORM fields normalized.
        """
        # -----------------------------------------------------------------
        # Rule 1: Both ranges numeric, both modifiers NI -> both EQ
        # -----------------------------------------------------------------
        rule_1: Column = (
            _is_numeric_value(F.col("NORM_RANGE_LOW"))
            & _is_numeric_value(F.col("NORM_RANGE_HIGH"))
            & (F.col("NORM_MODIFIER_LOW") == "NI")
            & (F.col("NORM_MODIFIER_HIGH") == "NI")
        )

        df = df.withColumn(
            "NORM_MODIFIER_LOW",
            F.when(rule_1, F.lit("EQ")).otherwise(F.col("NORM_MODIFIER_LOW")),
        ).withColumn(
            "NORM_MODIFIER_HIGH",
            F.when(rule_1, F.lit("EQ")).otherwise(F.col("NORM_MODIFIER_HIGH")),
        )

        # -----------------------------------------------------------------
        # Rule 2: Both modifiers EQ but only one range populated
        # -----------------------------------------------------------------
        both_eq: Column = (F.col("NORM_MODIFIER_LOW") == "EQ") & (
            F.col("NORM_MODIFIER_HIGH") == "EQ"
        )

        # 2a: EQ/EQ + only LOW numeric -> lower-only pattern
        rule_2a: Column = (
            both_eq
            & _is_numeric_value(F.col("NORM_RANGE_LOW"))
            & _is_null_or_empty_or_ni(F.col("NORM_RANGE_HIGH"))
        )

        # 2b: EQ/EQ + only HIGH numeric -> upper-only pattern
        rule_2b: Column = (
            both_eq
            & _is_numeric_value(F.col("NORM_RANGE_HIGH"))
            & _is_null_or_empty_or_ni(F.col("NORM_RANGE_LOW"))
        )

        df = (
            df.withColumn(
                "NORM_MODIFIER_LOW",
                F.when(rule_2a, F.lit("GE"))
                .when(rule_2b, F.lit("NO"))
                .otherwise(F.col("NORM_MODIFIER_LOW")),
            )
            .withColumn(
                "NORM_MODIFIER_HIGH",
                F.when(rule_2a, F.lit("NO"))
                .when(rule_2b, F.lit("LE"))
                .otherwise(F.col("NORM_MODIFIER_HIGH")),
            )
            .withColumn(
                "NORM_RANGE_HIGH",
                F.when(rule_2a, F.lit(None)).otherwise(F.col("NORM_RANGE_HIGH")),
            )
            .withColumn(
                "NORM_RANGE_LOW",
                F.when(rule_2b, F.lit(None)).otherwise(F.col("NORM_RANGE_LOW")),
            )
        )

        # -----------------------------------------------------------------
        # Rule 3: Upper-only cleanup
        # MOD_HIGH in (LT,LE) + RANGE_LOW is NI/null/empty
        #   -> RANGE_LOW=null, MOD_LOW=NO
        # -----------------------------------------------------------------
        rule_3: Column = F.col("NORM_MODIFIER_HIGH").isin("LT", "LE") & _is_null_or_empty_or_ni(
            F.col("NORM_RANGE_LOW")
        )

        df = df.withColumn(
            "NORM_RANGE_LOW",
            F.when(rule_3, F.lit(None)).otherwise(F.col("NORM_RANGE_LOW")),
        ).withColumn(
            "NORM_MODIFIER_LOW",
            F.when(rule_3, F.lit("NO")).otherwise(F.col("NORM_MODIFIER_LOW")),
        )

        # -----------------------------------------------------------------
        # Rule 4: Lower-only cleanup
        # MOD_LOW in (GT,GE) + RANGE_HIGH is NI/null/empty
        #   -> RANGE_HIGH=null, MOD_HIGH=NO
        # -----------------------------------------------------------------
        rule_4: Column = F.col("NORM_MODIFIER_LOW").isin("GT", "GE") & _is_null_or_empty_or_ni(
            F.col("NORM_RANGE_HIGH")
        )

        df = df.withColumn(
            "NORM_RANGE_HIGH",
            F.when(rule_4, F.lit(None)).otherwise(F.col("NORM_RANGE_HIGH")),
        ).withColumn(
            "NORM_MODIFIER_HIGH",
            F.when(rule_4, F.lit("NO")).otherwise(F.col("NORM_MODIFIER_HIGH")),
        )

        return df
