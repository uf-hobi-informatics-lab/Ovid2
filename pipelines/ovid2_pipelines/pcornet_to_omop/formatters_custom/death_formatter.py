"""OMOP ``death`` formatter (PCORnet ``DEATH`` + ``DEATH_CAUSE`` -> OMOP).

Ported from the legacy converter's ``15.death_mapper.py``. Team decision:
``person_id = PATID`` (no id-mapping table). ``death_type_concept_id``
comes from the static ``death_cause_to_death_type_concept_id_dict``; the
cause concept ids are resolved live against the ``concept`` /
``concept_relationship`` shared views (domain = Condition) using the same
:func:`resolve_concepts` engine the M1 ``condition_occurrence`` formatter
uses.

DEATH_CAUSE is an optional sibling: when absent, ``death`` still emits one
row per DEATH row with null cause columns (a left join, unlike
``condition_occurrence``'s inner filter — a patient death is recorded even
without a coded cause).

Output columns match ``omop_5_3.DEATH`` (10 columns incl. the Ovid
trailing bookkeeping fields). Fully lazy — no ``.collect()`` / ``.count()``.
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


class DeathFormatter(BaseFormatter):
    """Map PCORnet DEATH (+ DEATH_CAUSE) into OMOP death."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "death"

    @property
    def input_file_name(self) -> str:
        """Primary source identifier (DEATH); DEATH_CAUSE read as sibling."""
        return "DEATH"

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
        """Reshape DEATH + DEATH_CAUSE into OMOP death (lazy)."""
        fns = self.context.functions
        engine = self.context.engine
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        spark = self.context.spark

        # --- DEATH leg -----------------------------------------------------
        death = df.select(
            col("patid").alias("person_id"),
            col("death_date").alias("death_src"),
            literal_map(
                fns,
                _dicts.death_cause_to_death_type_concept_id_dict,
                upper(trim(col("death_source"))),
            ).alias("death_type_concept_id"),
            col("source").alias("source"),
        )

        # --- DEATH_CAUSE leg (optional sibling) ----------------------------
        # Legacy joins DEATH_CAUSE on PATID and resolves DEATH_CAUSE against
        # the Condition domain. Missing DEATH_CAUSE -> null cause columns.
        cause_code_col = "__dc_code"
        try:
            cause_raw = self._read_sibling("DEATH_CAUSE")
            cause = cause_raw.select(
                col("patid").alias("__dc_patid"),
                col("death_cause").alias(cause_code_col),
            )
            # A DEATH_CAUSE row carries no reliable code_type; the legacy
            # converter resolves it against the Condition domain on code
            # alone (DEATH_CAUSE == condition_code), so match on code only.
            resolved_cause = resolve_concepts(
                engine.with_column(cause, "__dc_code_type", lit("NI")),
                spark,
                functions=fns,
                engine=engine,
                domain=_DOMAIN,
                prefix="cause",
                code_column=cause_code_col,
                code_type_column="__dc_code_type",
                require_source_concept=False,
                match_code_type=False,
            ).select(
                col("__dc_patid"),
                col(cause_code_col).alias("cause_source_value"),
                col("cause_concept_id"),
                col("cause_source_concept_id"),
            )
            death = death.join(
                resolved_cause,
                death["person_id"] == resolved_cause["__dc_patid"],
                "left",
            ).drop("__dc_patid")
        except FileNotFoundError:
            death = engine.with_column(death, "cause_source_value", lit(""))
            death = engine.with_column(death, "cause_concept_id", lit(""))
            death = engine.with_column(death, "cause_source_concept_id", lit(""))

        return death.select(
            col("person_id").alias("person_id"),
            default_date(fns, "death_src").alias("death_date"),
            default_datetime(fns, "death_src").alias("death_datetime"),
            col("death_type_concept_id").alias("death_type_concept_id"),
            col("cause_concept_id").alias("cause_concept_id"),
            col("cause_source_value").alias("cause_source_value"),
            col("cause_source_concept_id").alias("cause_source_concept_id"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("DEATH").alias("mapped_from"),
        )
