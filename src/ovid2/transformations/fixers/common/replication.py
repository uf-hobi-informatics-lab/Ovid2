"""Fix: Replicate ADMIT_DATE and ENC_TYPE from the encounter table.

Summary:
    Ensures that every encounter-linked clinical table carries the
    authoritative ADMIT_DATE and ENC_TYPE values from the encounter table.
    Many downstream fixes depend on these fields (e.g., ``illogical_admit_date``
    checks dates relative to ADMIT_DATE, and EDC reports stratify by
    ENC_TYPE).  This fix overwrites whatever values the source system
    provided with the encounter table's values, guaranteeing cross-table
    consistency.

Use Cases:
    - Replicate admit date and encounter type into the diagnosis table so
      that subsequent date-validation fixes can reference ADMIT_DATE.
      Config::

          {"fix": "replication", "src1": "encounter"}

    - Applied to any encounter-linked table that needs consistent
      ADMIT_DATE / ENC_TYPE: diagnosis, procedures, prescribing, med_admin,
      lab_result_cm, obs_clin, obs_gen, immunization, etc.
      Config::

          {"fix": "replication", "src1": "encounter"}

Deployment:
    1. Add the fix dict to the target table's entry in ``FIXER_CHAINS`` in
       ``pipelines/<pipeline>/fixer_configs.py``.  Place **after** the orphan
       fixes (``patid_orphans``, ``encounterid_orphans``, ``providerid_orphans``)
       and **before** date-validation fixes (``illogical_admit_date``,
       ``illogical_discharge_date``, ``copy_admit_date_or_delete``) which
       depend on the replicated ADMIT_DATE.

       Example (``fixer_configs.py``)::

           FIXER_CHAINS = {
               "diagnosis": [
                   {"fix": "patid_orphans", "src1": "demographic"},
                   {"fix": "encounterid_orphans", "src1": "encounter"},
                   {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"},
                   {"fix": "replication", "src1": "encounter"},
                   {"fix": "illogical_admit_date", "src1": "DX_DATE"},
               ],
           }

    2. Run the pipeline with ``fix`` in the transformations list::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables diagnosis --transformations fix \\
               --input_address disc_input --output_address disc_output

    3. Or include it in a full pipeline run::

           python3 ovid.py etl \\
               --pipeline demo --batch_id 20260319 \\
               --tables all --transformations all \\
               --input_address disc_input --output_address disc_output

    Prerequisites:
        - The ``encounter`` table must be processed and registered as a
          Spark temp view **before** the target table.
        - The target DataFrame must already have ``ADMIT_DATE`` and
          ``ENC_TYPE`` columns (even if their values are stale or null).
"""

from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql.functions import broadcast, col

from ovid2.transformations.fixers.base import BaseFixer


class ReplicationFixer(BaseFixer):
    """Copy ADMIT_DATE and ENC_TYPE from the encounter table into the main table.

    Performs a left join against the encounter table specified in
    ``context['src1']`` on ENCOUNTERID. Overwrites the existing ADMIT_DATE
    and ENC_TYPE columns in the main table with the values from the encounter
    table. Temporary join columns are dropped after the operation.

    All operations are lazy Spark DataFrame transformations.
    """

    @property
    def name(self) -> str:
        """Return the fix name identifier."""
        return "replication"

    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Replicate ADMIT_DATE and ENC_TYPE from the encounter table.

        Args:
            df: The DataFrame to fix.
            context: Dictionary containing:
                - ``input_address``: Address for reading reference tables.
                - ``spark``: SparkSession instance.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Encounter table name to read from.

        Returns:
            DataFrame with ADMIT_DATE and ENC_TYPE overwritten from encounter.
        """
        encounter_table: str = context["src1"]

        encounter_df: DataFrame = context["spark"].table(encounter_table)

        encounter_ref: DataFrame = encounter_df.select(
            col("ENCOUNTERID").alias("_enc_ref_id"),
            col("ADMIT_DATE").alias("_enc_admit_date"),
            col("ENC_TYPE").alias("_enc_enc_type"),
        )

        joined: DataFrame = df.join(
            broadcast(encounter_ref),
            on=(col("ENCOUNTERID") == col("_enc_ref_id")),
            how="left",
        )

        result: DataFrame = (
            joined.withColumn("ADMIT_DATE", col("_enc_admit_date"))
            .withColumn("ENC_TYPE", col("_enc_enc_type"))
            .drop("_enc_ref_id", "_enc_admit_date", "_enc_enc_type")
        )

        return result
