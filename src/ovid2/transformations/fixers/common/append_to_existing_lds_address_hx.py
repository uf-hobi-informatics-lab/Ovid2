"""Fix: Append current LDS address history with existing data from Snowflake.

Summary:
    Merges the current batch's LDS address history with previously existing
    data from Snowflake, enabling longitudinal address tracking across
    batches.  For patients only in one dataset (new or existing), their
    records pass through unchanged.  For patients present in both, the fix
    adjusts ``ADDRESS_PERIOD_START`` and ``ADDRESS_PERIOD_END`` based on
    whether ZIP5 codes match: same ZIP5 extends the period boundaries;
    different ZIP5 indicates a move and adjusts the end date.  Applied
    exclusively to the ``lds_address_history`` table.

    .. note::
        **This fix is currently a pass-through stub.**  The full merge
        logic requires the existing LDS data to be provided in
        ``context['existing_lds_df']`` via a Snowflake address read during
        the fix stage, which is not yet implemented.

Use Cases:
    - Merge new batch LDS address history with existing Snowflake data
      (once the Snowflake read integration is implemented).
      Config::

          {"fix": "append_to_existing_lds_address_hx", "src1": "sf_server", "src2": "CHAR_DEMO"}

    - Currently a no-op -- the fix returns the DataFrame unchanged until
      Snowflake integration is wired in.

Deployment:
    1. Add the fix dict to the ``lds_address_history`` table's entry in
       ``FIXER_CHAINS`` in ``pipelines/<pipeline>/fixer_configs.py``.
       Should be placed as the **last** fix for ``lds_address_history``
       (after all other data quality fixes are applied).

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "lds_address_history": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "append_to_existing_lds_address_hx", "src1": "sf_server", "src2": "CHAR_DEMO"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260101 \\
               --tables lds_address_history --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260101 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - **Not yet functional** -- requires ``context['existing_lds_df']``
          to be populated by the pipeline orchestrator via a Snowflake
          address read.
        - When implemented: the output Snowflake address must be configured
          and accessible, and the existing LDS_ADDRESS_HISTORY table must
          exist in the target database.
"""

from typing import Any

from pyspark.sql import DataFrame

from ovid2.transformations.fixers.base import BaseFixer


class AppendToExistingLdsAddressHxFixer(BaseFixer):
    """Append existing LDS address history from Snowflake to the current batch.

    This fix merges the current batch's LDS address history with previously
    existing data from Snowflake, handling:
    - Patients only in the existing data (passed through unchanged)
    - Patients only in the new batch (passed through unchanged)
    - Shared patients with different ZIP5 (period end date adjusted)
    - Shared patients with same ZIP5 (period start/end dates adjusted)

    .. note::
        Currently implemented as a pass-through. The full implementation
        requires the existing LDS data to be provided in
        ``context['existing_lds_df']`` via a Snowflake address read.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "append_to_existing_lds_address_hx"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Append existing LDS address history to the current batch.

        Args:
            df: The LDS address history DataFrame to fix.
            context: Dictionary containing:
                - ``src1``: Snowflake DB server name.
                - ``src2``: Snowflake DB name.
                - ``existing_lds_df`` (optional): Pre-loaded DataFrame of
                  existing LDS_ADDRESS_HISTORY from Snowflake. If not
                  provided, the fix returns ``df`` unchanged.

        Returns:
            DataFrame with existing LDS address history merged in, or
            ``df`` unchanged if no existing data is available.
        """
        # TODO: Implement full merge logic once Snowflake address reads are
        # available during the fix stage. The existing data should be provided
        # in context['existing_lds_df'] by the pipeline orchestrator.
        #
        # Full implementation should:
        # 1. Separate patients only in existing, only in new, and shared
        # 2. For shared patients with different ZIP5: set ADDRESS_PERIOD_END
        #    to the later of the two ADDRESS_PERIOD_START dates
        # 3. For shared patients with same ZIP5: set ADDRESS_PERIOD_START to
        #    the earlier date and ADDRESS_PERIOD_END to the later date
        # 4. Union all four groups

        return df
