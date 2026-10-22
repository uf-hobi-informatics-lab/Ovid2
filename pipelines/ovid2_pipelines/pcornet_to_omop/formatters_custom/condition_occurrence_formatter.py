"""OMOP ``condition_occurrence`` formatter.

Fan-in of PCORnet ``DIAGNOSIS`` + ``CONDITION`` into OMOP
``condition_occurrence`` (domain = Condition). Ported from the legacy
converter's ``09.diagnosis_to_common`` + ``08.condition_to_common`` +
``16.from_common_to_condition_occurrence`` chain, collapsed into one
direct formatter (no persisted ``common`` staging table).

Team decisions baked in:

* ``condition_occurrence_id`` = the PCORnet surrogate id (``DIAGNOSISID``
  for diagnosis-sourced rows, ``CONDITIONID`` for condition-sourced
  rows).
* ``person_id`` = PATID, ``visit_occurrence_id`` = ENCOUNTERID.
* concept ids resolved live against the ``concept`` /
  ``concept_relationship`` shared views (domain = Condition).

Only rows whose code resolves to a valid Condition-domain source concept
survive (the legacy ``filter_common_table`` inner join). Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.adapters._sources import read_source
from ovid2.formatters.base import BaseFormatter
from ovid2.formatters.spec import SourceSpec

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._concepts import resolve_concepts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import (
    default_date,
    default_datetime,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

_DOMAIN = "Condition"


class ConditionOccurrenceFormatter(BaseFormatter):
    """Map PCORnet DIAGNOSIS + CONDITION into OMOP condition_occurrence."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "condition_occurrence"

    @property
    def input_file_name(self) -> str:
        """Primary source identifier (DIAGNOSIS); CONDITION read as sibling."""
        return "DIAGNOSIS"

    def _read_sibling(self, identifier: str) -> "DataFrame":
        ctx = self.context
        return read_source(
            SourceSpec(name=identifier, glob=identifier, lowercase_columns=True),
            spark=ctx.spark,
            input_address=ctx.input_address,
            pipeline=ctx.pipeline,
            batch_id=ctx.batch_id,
            engine=ctx.engine,
            input_address_name=ctx.input_address_name,
        )

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape DIAGNOSIS + CONDITION into OMOP condition_occurrence (lazy)."""
        fns = self.context.functions
        engine = self.context.engine
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        when = fns.when
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        spark = self.context.spark

        # --- DIAGNOSIS leg -------------------------------------------------
        dx_type = upper(trim(col("dx_type")))
        dx_type = when(dx_type.isin("OT", "UN"), lit("NI")).otherwise(dx_type)
        diagnosis = df.select(
            col("diagnosisid").alias("cc_id"),
            col("patid").alias("person_id"),
            col("encounterid").alias("visit_occurrence_id"),
            col("dx").alias("cond_code"),
            dx_type.alias("cond_code_type"),
            col("admit_date").alias("start_src"),
            col("dx_date").alias("end_src"),
            literal_map(
                fns, _dicts.diagnosis_type_concept_id_dict, upper(trim(col("dx_origin")))
            ).alias("condition_type_concept_id"),
            col("dx_source").alias("condition_status_source_value"),
            literal_map(
                fns,
                _dicts.diagnosis_status_concept_id_dict,
                upper(trim(col("dx_source"))),
            ).alias("condition_status_concept_id"),
            col("source").alias("source"),
            lit("DIAGNOSIS").alias("mapped_from"),
        )

        rows = diagnosis

        # --- CONDITION leg (optional sibling) ------------------------------
        try:
            cond_raw = self._read_sibling("CONDITION")
            cond_type = upper(trim(col("condition_type")))
            cond_type = when(cond_type.isin("OT", "UN"), lit("NI")).otherwise(cond_type)
            condition = cond_raw.select(
                col("conditionid").alias("cc_id"),
                col("patid").alias("person_id"),
                col("encounterid").alias("visit_occurrence_id"),
                col("condition").alias("cond_code"),
                cond_type.alias("cond_code_type"),
                col("report_date").alias("start_src"),
                col("resolve_date").alias("end_src"),
                literal_map(
                    fns,
                    _dicts.condition_type_concept_id_dict,
                    upper(trim(col("condition_source"))),
                ).alias("condition_type_concept_id"),
                col("condition_status").alias("condition_status_source_value"),
                literal_map(
                    fns,
                    _dicts.condition_status_concept_id_dict,
                    upper(trim(col("condition_status"))),
                ).alias("condition_status_concept_id"),
                col("source").alias("source"),
                lit("CONDITION").alias("mapped_from"),
            )
            rows = engine.union_by_name(diagnosis, condition, allow_missing_columns=True)
        except FileNotFoundError:
            pass

        resolved = resolve_concepts(
            rows,
            spark,
            functions=fns,
            engine=engine,
            domain=_DOMAIN,
            prefix="cond",
            code_column="cond_code",
            code_type_column="cond_code_type",
            require_source_concept=True,
        )

        return resolved.select(
            col("cc_id").alias("condition_occurrence_id"),
            col("person_id").alias("person_id"),
            col("cond_concept_id").alias("condition_concept_id"),
            default_date(fns, "start_src").alias("condition_start_date"),
            default_datetime(fns, "start_src").alias("condition_start_datetime"),
            default_date(fns, "end_src").alias("condition_end_date"),
            default_datetime(fns, "end_src").alias("condition_end_datetime"),
            col("condition_type_concept_id").alias("condition_type_concept_id"),
            col("condition_status_concept_id").alias("condition_status_concept_id"),
            lit("").alias("stop_reason"),
            lit("").alias("provider_id"),
            col("visit_occurrence_id").alias("visit_occurrence_id"),
            lit("").alias("visit_detail_id"),
            col("cond_code").alias("condition_source_value"),
            col("cond_source_concept_id").alias("condition_source_concept_id"),
            col("condition_status_source_value").alias("condition_status_source_value"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            col("mapped_from").alias("mapped_from"),
        )
