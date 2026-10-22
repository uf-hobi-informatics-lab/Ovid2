"""OMOP ``specimen`` formatter (PCORnet ``LAB_RESULT_CM`` specimen fields).

Ported from the legacy converter's ``14.specimen_mapper.py``. PCORnet has
no dedicated specimen table; the specimen fields ride on ``LAB_RESULT_CM``.

Team decisions: ``specimen_id`` = ``LAB_RESULT_CM_ID`` (the surrogate id
the legacy ``mapping_lab_result_cm_specimen_to_omop_id`` table was keyed
on), ``person_id`` = PATID. ``specimen_concept_id`` from the static
``specimen_to_specimen_concept_id_dict``; the other concept ids are the
legacy ``lit(0)`` placeholders. Fully lazy.
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


class SpecimenFormatter(BaseFormatter):
    """Map PCORnet LAB_RESULT_CM specimen fields into OMOP specimen."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "specimen"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier."""
        return "LAB_RESULT_CM"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape LAB_RESULT_CM specimen fields into OMOP specimen (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        return df.select(
            col("lab_result_cm_id").alias("specimen_id"),
            col("patid").alias("person_id"),
            literal_map(
                fns,
                _dicts.specimen_to_specimen_concept_id_dict,
                upper(trim(col("specimen_source"))),
            ).alias("specimen_concept_id"),
            lit("0").alias("specimen_type_concept_id"),
            default_date(fns, "specimen_date").alias("specimen_date"),
            default_datetime(fns, "specimen_date").alias("specimen_datetime"),
            lit("").alias("quantity"),
            lit("0").alias("unit_concept_id"),
            lit("0").alias("anatomic_site_concept_id"),
            lit("0").alias("disease_status_concept_id"),
            col("lab_result_cm_id").alias("specimen_source_id"),
            col("specimen_source").alias("specimen_source_value"),
            lit("").alias("unit_source_value"),
            lit("").alias("anatomic_site_source_value"),
            lit("").alias("disease_status_source_value"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("LAB_RESULT_CM").alias("mapped_from"),
        )
