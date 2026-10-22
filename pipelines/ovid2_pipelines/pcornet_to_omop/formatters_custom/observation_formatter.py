"""OMOP ``observation`` formatter (coded-clinical fan-in, domain=Observation).

Reproduces the legacy converter's fan-in + ``19.from_common_to_observation``
domain split. Shares :func:`_common.build_common` with the measurement /
procedure_occurrence formatters; :func:`_concepts.resolve_concepts` filters
the unified common DataFrame to the Observation domain (inner join).

Team decisions: ``observation_id`` = the PCORnet surrogate id
(``common_id``), ``person_id`` = PATID, ``visit_occurrence_id`` =
ENCOUNTERID. Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop.formatters_custom import _common
from ovid2_pipelines.pcornet_to_omop.formatters_custom._concepts import resolve_concepts

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

_DOMAIN = "Observation"


class ObservationFormatter(BaseFormatter):
    """Map the coded-clinical PCORnet sources into OMOP observation."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "observation"

    @property
    def input_file_name(self) -> str:
        """Primary source (LAB_RESULT_CM); other sources read as siblings."""
        return "LAB_RESULT_CM"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Fan-in + Observation-domain filter + project (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit

        common = _common.build_common(df, self.context, table_name=self.table_name)
        resolved = resolve_concepts(
            common,
            self.context.spark,
            functions=fns,
            engine=self.context.engine,
            domain=_DOMAIN,
            prefix="obs",
            code_column="common_data_source_value",
            code_type_column="pcornet_code_type",
            require_source_concept=True,
        )

        return resolved.select(
            col("common_id").alias("observation_id"),
            col("person_id").alias("person_id"),
            col("obs_concept_id").alias("observation_concept_id"),
            col("event_start_date").alias("observation_date"),
            col("event_start_datetime").alias("observation_datetime"),
            col("common_data_type_concept_id").alias("observation_type_concept_id"),
            col("value_as_number").alias("value_as_number"),
            col("value_as_string").alias("value_as_string"),
            col("value_as_concept_id").alias("value_as_concept_id"),
            col("qualifier_concept_id").alias("qualifier_concept_id"),
            col("unit_concept_id").alias("unit_concept_id"),
            col("provider_id").alias("provider_id"),
            col("visit_occurrence_id").alias("visit_occurrence_id"),
            col("visit_detail_id").alias("visit_detail_id"),
            col("common_data_source_value").alias("observation_source_value"),
            col("obs_source_concept_id").alias("observation_source_concept_id"),
            col("unit_source_value").alias("unit_source_value"),
            col("qualifier_source_value").alias("qualifier_source_value"),
            col("value_as_concept_id").alias("value_source_concept_id"),
            col("value_source_value").alias("value_source_value"),
            lit("").alias("questionnaire_response_id"),
            col("updated").alias("updated"),
            col("source").alias("source"),
            col("mapped_from").alias("mapped_from"),
        )
