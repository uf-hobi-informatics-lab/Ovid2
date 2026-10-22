"""example_pcornet harvest formatter — one-row metadata table.

Reads the encounter table to find the maximum admit_date, caps it at
today, then materialises a single-row PCORnet harvest DataFrame using
pipeline-level constants.

This formatter is deliberately *eager* — it calls ``.agg().collect()``
on a single aggregated value, then constructs a one-row DataFrame from a
Python dict. Both operations are annotated with ``# noqa: OVID2001``
because they are the only justified violations of the lazy-Spark rule.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


INPUT_FILE_NAME: str = "*Encounter*"


class HarvestFormatter(BaseFormatter):
    """Formatter for the example_pcornet ``harvest`` table (single-row output)."""

    @property
    def table_name(self) -> str:
        """Return the CDM table name."""
        return "harvest"

    @property
    def input_file_name(self) -> str:
        """Return the input file glob."""
        return INPUT_FILE_NAME

    def format(self, df: "DataFrame") -> "DataFrame":  # noqa: A003 — CDM API
        """Build the one-row harvest DataFrame from the encounter feed."""
        from pyspark.sql.functions import max as spark_max

        from ovid2.utils.common import format_date_col
        from ovid2_pipelines.example_pcornet import pipeline_settings as ps

        spark = df.sparkSession
        admit_date_df = df.select(format_date_col(df["ADMIT_DATE"]).alias("admit_date"))
        max_row = admit_date_df.agg(  # noqa: OVID2001 — single aggregated value
            spark_max("admit_date").alias("max_date")
        ).collect()[0]
        max_admit_date = max_row["max_date"]

        today = date.today().strftime("%Y-%m-%d")
        harvest_date = today if max_admit_date is None or max_admit_date > today else max_admit_date

        harvest_row: dict[str, str] = {
            "NETWORKID": ps.NETWORKID,
            "NETWORK_NAME": ps.NETWORK_NAME,
            "DATAMARTID": ps.DATAMARTID,
            "DATAMART_NAME": ps.DATAMART_NAME,
            "DATAMART_PLATFORM": ps.DATAMART_PLATFORM,
            "CDM_VERSION": ps.CDM_VERSION,
            "DATAMART_CLAIMS": ps.DATAMART_CLAIMS,
            "DATAMART_EHR": ps.DATAMART_EHR,
        }
        # MGMT fields — same value across every date column.
        for column in (
            "BIRTH_DATE_MGMT",
            "ENR_START_DATE_MGMT",
            "ENR_END_DATE_MGMT",
            "ADMIT_DATE_MGMT",
            "DISCHARGE_DATE_MGMT",
            "DX_DATE_MGMT",
            "PX_DATE_MGMT",
            "RX_ORDER_DATE_MGMT",
            "RX_START_DATE_MGMT",
            "RX_END_DATE_MGMT",
            "DISPENSE_DATE_MGMT",
            "LAB_ORDER_DATE_MGMT",
            "SPECIMEN_DATE_MGMT",
            "RESULT_DATE_MGMT",
            "MEASURE_DATE_MGMT",
            "ONSET_DATE_MGMT",
            "REPORT_DATE_MGMT",
            "RESOLVE_DATE_MGMT",
            "PRO_DATE_MGMT",
            "DEATH_DATE_MGMT",
            "MEDADMIN_START_DATE_MGMT",
            "MEDADMIN_STOP_DATE_MGMT",
            "OBSCLIN_START_DATE_MGMT",
            "OBSCLIN_STOP_DATE_MGMT",
            "OBSGEN_START_DATE_MGMT",
            "OBSGEN_STOP_DATE_MGMT",
            "ADDRESS_PERIOD_START_MGMT",
            "ADDRESS_PERIOD_END_MGMT",
            "VX_RECORD_DATE_MGMT",
            "VX_ADMIN_DATE_MGMT",
            "VX_EXP_DATE_MGMT",
        ):
            harvest_row[column] = ps.MGMT_FIELDS_VALUE

        for column in (
            "REFRESH_DEMOGRAPHIC_DATE",
            "REFRESH_ENROLLMENT_DATE",
            "REFRESH_ENCOUNTER_DATE",
            "REFRESH_DIAGNOSIS_DATE",
            "REFRESH_PROCEDURES_DATE",
            "REFRESH_VITAL_DATE",
            "REFRESH_DISPENSING_DATE",
            "REFRESH_LAB_RESULT_CM_DATE",
            "REFRESH_CONDITION_DATE",
            "REFRESH_PRO_CM_DATE",
            "REFRESH_PRESCRIBING_DATE",
            "REFRESH_PCORNET_TRIAL_DATE",
            "REFRESH_DEATH_DATE",
            "REFRESH_DEATH_CAUSE_DATE",
            "REFRESH_MED_ADMIN_DATE",
            "REFRESH_OBS_CLIN_DATE",
            "REFRESH_PROVIDER_DATE",
            "REFRESH_OBS_GEN_DATE",
            "REFRESH_HASH_TOKEN_DATE",
            "REFRESH_LDS_ADDRESS_HX_DATE",
            "REFRESH_IMMUNIZATION_DATE",
        ):
            harvest_row[column] = harvest_date

        return spark.createDataFrame([harvest_row])
