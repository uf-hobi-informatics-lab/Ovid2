"""OMOP ``measurement`` formatter (coded-clinical fan-in, domain=Measurement).

Reproduces the legacy converter's
``06/07/10/11/12.*_to_common`` fan-in + ``18.from_common_to_measurement``
domain split, collapsed into one direct formatter (no persisted ``common``
staging table).

The shared :func:`_common.build_common` unions every available coded
clinical source (LAB_RESULT_CM + PROCEDURES + OBS_CLIN + OBS_GEN + VITAL)
into one lazy DataFrame; :func:`_concepts.resolve_concepts` then filters it
to the Measurement domain (inner join on the concept view = "only rows
whose code exists as a Measurement-domain concept survive"), exactly as the
legacy ``filter_common_table`` inner join did.

Team decisions: ``measurement_id`` = the PCORnet surrogate id (``common_id``,
i.e. LAB_RESULT_CM_ID / PROCEDURESID / OBSCLINID / OBSGENID / VITALID+measure),
``person_id`` = PATID, ``visit_occurrence_id`` = ENCOUNTERID. Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop.formatters_custom import _common
from ovid2_pipelines.pcornet_to_omop.formatters_custom._concepts import resolve_concepts

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

_DOMAIN = "Measurement"


class MeasurementFormatter(BaseFormatter):
    """Map the coded-clinical PCORnet sources into OMOP measurement."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "measurement"

    @property
    def input_file_name(self) -> str:
        """Primary source (LAB_RESULT_CM); other sources read as siblings."""
        return "LAB_RESULT_CM"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Fan-in + Measurement-domain filter + project (lazy)."""
        fns = self.context.functions
        col = fns.col

        common = _common.build_common(df, self.context, table_name=self.table_name)
        resolved = resolve_concepts(
            common,
            self.context.spark,
            functions=fns,
            engine=self.context.engine,
            domain=_DOMAIN,
            prefix="meas",
            code_column="common_data_source_value",
            code_type_column="pcornet_code_type",
            require_source_concept=True,
        )

        return resolved.select(
            col("common_id").alias("measurement_id"),
            col("person_id").alias("person_id"),
            col("meas_concept_id").alias("measurement_concept_id"),
            col("event_start_date").alias("measurement_date"),
            col("event_start_datetime").alias("measurement_datetime"),
            col("event_start_time").alias("measurement_time"),
            col("common_data_type_concept_id").alias("measurement_type_concept_id"),
            col("operator_concept_id").alias("operator_concept_id"),
            col("value_as_number").alias("value_as_number"),
            col("value_as_concept_id").alias("value_as_concept_id"),
            col("unit_concept_id").alias("unit_concept_id"),
            col("range_low").alias("range_low"),
            col("range_high").alias("range_high"),
            col("provider_id").alias("provider_id"),
            col("visit_occurrence_id").alias("visit_occurrence_id"),
            col("visit_detail_id").alias("visit_detail_id"),
            col("common_data_source_value").alias("measurement_source_value"),
            col("meas_source_concept_id").alias("measurement_source_concept_id"),
            col("unit_source_value").alias("unit_source_value"),
            col("value_source_value").alias("value_source_value"),
            col("updated").alias("updated"),
            col("source").alias("source"),
            col("mapped_from").alias("mapped_from"),
        )
