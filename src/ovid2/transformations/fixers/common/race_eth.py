"""Fix: Derive RACE_ETH_* columns from PCORnet RACE and HISPANIC codes.

Summary:
    Adds seven derived race/ethnicity indicator columns required by the
    PCORnet CDM.  These columns combine RACE and HISPANIC values into
    mutually exclusive categories used for cohort stratification and
    reporting.  The derivation follows the PCORnet specification: each
    race category is ``Y`` only when HISPANIC is ``N`` (not Hispanic),
    except ``RACE_ETH_HISPANIC`` which is ``Y`` when HISPANIC is ``Y``
    regardless of race.  ``RACE_ETH_MISSING`` captures refused, other,
    unknown, and no-information combinations.  Applied exclusively to the
    ``demographic`` table.  No rows are added or deleted.

Use Cases:
    - Standard race/ethnicity derivation on the demographic table using
      the default column names.
      Config::

          {"fix": "race_eth", "src1": "RACE", "src2": "HISPANIC"}

    - The fix uses ``RACE`` and ``HISPANIC`` by default, so ``src1`` and
      ``src2`` can be omitted if the columns follow the standard naming.
      Config::

          {"fix": "race_eth"}

Deployment:
    1. Add the fix dict to the ``demographic`` table's entry in
       ``FIXER_CHAINS`` in ``pipelines/<pipeline>/fixer_configs.py``.  This
       should typically be the **first** (or only) fix for demographic,
       since it adds columns that other processes may need.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "demographic": [
                   {"fix": "race_eth", "src1": "RACE", "src2": "HISPANIC"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables demographic --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20251212 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The DataFrame must contain ``RACE`` and ``HISPANIC`` columns
          (or the columns named in ``src1``/``src2``).
        - No cross-table dependencies.  This fix is self-contained.
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, when

from ovid2.transformations.fixers.base import BaseFixer


class RaceEthFixer(BaseFixer):
    """Derive seven RACE_ETH_* columns from RACE and HISPANIC values.

    Adds the following columns based on PCORnet CDM race and Hispanic codes:

    - ``RACE_ETH_AI_AN``: American Indian / Alaska Native (RACE=01, HISPANIC=N)
    - ``RACE_ETH_ASIAN``: Asian (RACE=02, HISPANIC=N)
    - ``RACE_ETH_BLACK``: Black / African American (RACE=03, HISPANIC=N)
    - ``RACE_ETH_NH_PI``: Native Hawaiian / Pacific Islander (RACE=04, HISPANIC=N)
    - ``RACE_ETH_WHITE``: White (RACE=05, HISPANIC=N)
    - ``RACE_ETH_HISPANIC``: Hispanic (HISPANIC=Y, any race)
    - ``RACE_ETH_MISSING``: Cascading logic for refused, other, unknown, and
      missing combinations

    No rows are added or deleted.  All operations are lazy Spark DataFrame
    transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "race_eth"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Derive RACE_ETH_* columns from RACE and HISPANIC codes.

        Args:
            df: The DataFrame to fix.  Must contain RACE and HISPANIC columns.
            context: Dictionary containing:
                - ``src1``: Race column name (default ``"RACE"``).
                - ``src2``: Hispanic column name (default ``"HISPANIC"``).

        Returns:
            DataFrame with seven RACE_ETH_* columns appended.
        """
        race_col: str = context.get("src1", "RACE")
        hispanic_col: str = context.get("src2", "HISPANIC")

        race: col = col(race_col)
        hispanic: col = col(hispanic_col)

        non_hispanic = hispanic == lit("N")

        # Race/ethnicity indicator columns — Y when matching, N otherwise
        race_eth_mappings: dict[str, str] = {
            "RACE_ETH_AI_AN": "01",
            "RACE_ETH_ASIAN": "02",
            "RACE_ETH_BLACK": "03",
            "RACE_ETH_NH_PI": "04",
            "RACE_ETH_WHITE": "05",
        }

        result: DataFrame = df
        for column_name, race_code in race_eth_mappings.items():
            result = result.withColumn(
                column_name,
                when(
                    (race == lit(race_code)) & non_hispanic,
                    lit("Y"),
                ).otherwise(lit("N")),
            )

        # RACE_ETH_HISPANIC: Y when HISPANIC=Y regardless of race
        result = result.withColumn(
            "RACE_ETH_HISPANIC",
            when(hispanic == lit("Y"), lit("Y")).otherwise(lit("N")),
        )

        # RACE_ETH_MISSING: cascading logic
        #   RACE=R           → 'R'  (refused)
        #   RACE=OT, HISP=N  → 'OT' (other)
        #   RACE=UN           → 'UN' (unknown)
        #   RACE=NI, HISP=NI  → 'UN' (unknown)
        #   both null         → 'NI' (no information)
        #   else              → 'N'
        result = result.withColumn(
            "RACE_ETH_MISSING",
            when(race == lit("R"), lit("R"))
            .when((race == lit("OT")) & non_hispanic, lit("OT"))
            .when(race == lit("UN"), lit("UN"))
            .when(
                (race == lit("NI")) & (hispanic == lit("NI")),
                lit("UN"),
            )
            .when(race.isNull() & hispanic.isNull(), lit("NI"))
            .otherwise(lit("N")),
        )

        return result
