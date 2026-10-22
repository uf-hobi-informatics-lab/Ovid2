"""OMOP ``procedure_occurrence`` formatter (coded-clinical fan-in, domain=Procedure).

Reproduces the legacy converter's fan-in + ``17.from_common_to_procedure_occurrence``
domain split. Shares :func:`_common.build_common` with the measurement /
observation formatters; :func:`_concepts.resolve_concepts` filters the
unified common DataFrame to the Procedure domain (inner join).

Team decisions: ``procedure_occurrence_id`` = the PCORnet surrogate id
(``common_id``), ``person_id`` = PATID, ``visit_occurrence_id`` =
ENCOUNTERID, ``provider_id`` = PROVIDERID (carried from the source leg).
Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop.formatters_custom import _common
from ovid2_pipelines.pcornet_to_omop.formatters_custom._concepts import resolve_concepts

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

_DOMAIN = "Procedure"


class ProcedureOccurrenceFormatter(BaseFormatter):
    """Map the coded-clinical PCORnet sources into OMOP procedure_occurrence."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "procedure_occurrence"

    @property
    def input_file_name(self) -> str:
        """Primary source (LAB_RESULT_CM); other sources read as siblings."""
        return "LAB_RESULT_CM"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Fan-in + Procedure-domain filter + project (lazy)."""
        fns = self.context.functions
        col = fns.col

        common = _common.build_common(df, self.context, table_name=self.table_name)
        resolved = resolve_concepts(
            common,
            self.context.spark,
            functions=fns,
            engine=self.context.engine,
            domain=_DOMAIN,
            prefix="proc",
            code_column="common_data_source_value",
            code_type_column="pcornet_code_type",
            require_source_concept=True,
        )

        return resolved.select(
            col("common_id").alias("procedure_occurrence_id"),
            col("person_id").alias("person_id"),
            col("proc_concept_id").alias("procedure_concept_id"),
            col("event_start_date").alias("procedure_date"),
            col("event_start_datetime").alias("procedure_datetime"),
            col("common_data_type_concept_id").alias("procedure_type_concept_id"),
            col("qualifier_concept_id").alias("modifier_concept_id"),
            col("quantity").alias("quantity"),
            col("provider_id").alias("provider_id"),
            col("visit_occurrence_id").alias("visit_occurrence_id"),
            col("visit_detail_id").alias("visit_detail_id"),
            col("common_data_source_value").alias("procedure_source_value"),
            col("proc_source_concept_id").alias("procedure_source_concept_id"),
            col("qualifier_source_value").alias("modifier_source_value"),
            col("updated").alias("updated"),
            col("source").alias("source"),
            col("mapped_from").alias("mapped_from"),
        )
