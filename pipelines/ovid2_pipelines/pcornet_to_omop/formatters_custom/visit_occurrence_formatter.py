"""OMOP ``visit_occurrence`` formatter (PCORnet ``ENCOUNTER`` -> OMOP).

Ported from the legacy converter's ``02.visit_occurrence_mapper.py``.
Team decision: ``visit_occurrence_id = ENCOUNTERID``,
``person_id = PATID`` (no id-mapping tables). Encounter-type /
admitting-source / discharge-status concept ids come from the static
dictionaries in ``ovid2_pipelines.pcornet_to_omop.dictionaries``.

Output columns match ``omop_5_3.VISIT_OCCURRENCE`` (20 columns incl. the
Ovid trailing bookkeeping fields). Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import (
    default_date,
    default_datetime,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class VisitOccurrenceFormatter(BaseFormatter):
    """Map a PCORnet ENCOUNTER feed into the OMOP visit_occurrence table."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "visit_occurrence"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (Snowflake table / disc dir)."""
        return "ENCOUNTER"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape ENCOUNTER into OMOP visit_occurrence (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        coalesce = fns.coalesce
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        enc_type = upper(trim(col("enc_type")))
        adm_src = upper(trim(col("admitting_source")))
        dis_status = upper(trim(col("discharge_status")))

        return df.select(
            col("encounterid").alias("visit_occurrence_id"),
            col("patid").alias("person_id"),
            coalesce(
                literal_map(fns, _dicts.enc_type_to_visit_concept_id_dict, enc_type),
                lit("0"),
            ).alias("visit_concept_id"),
            default_date(fns, "admit_date").alias("visit_start_date"),
            default_datetime(fns, "admit_date").alias("visit_start_datetime"),
            default_date(fns, "discharge_date").alias("visit_end_date"),
            default_datetime(fns, "discharge_date").alias("visit_end_datetime"),
            lit("32035").alias("visit_type_concept_id"),  # Visit from EHR encounter
            col("providerid").alias("provider_id"),
            col("facilityid").alias("care_site_id"),
            col("enc_type").alias("visit_source_value"),
            coalesce(
                literal_map(fns, _dicts.enc_type_to_visit_concept_id_dict, enc_type),
                lit("0"),
            ).alias("visit_source_concept_id"),
            coalesce(
                literal_map(
                    fns,
                    _dicts.admitting_source_to_admitting_source_concept_id_dict,
                    adm_src,
                ),
                lit("0"),
            ).alias("admitting_source_concept_id"),
            col("admitting_source").alias("admitting_source_value"),
            coalesce(
                literal_map(
                    fns,
                    _dicts.discharge_status_to_discharge_concept_id_dict,
                    dis_status,
                ),
                lit("0"),
            ).alias("discharge_to_concept_id"),
            col("discharge_status").alias("discharge_to_source_value"),
            lit("").alias("preceding_visit_occurrence_id"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("ENCOUNTER").alias("mapped_from"),
        )
