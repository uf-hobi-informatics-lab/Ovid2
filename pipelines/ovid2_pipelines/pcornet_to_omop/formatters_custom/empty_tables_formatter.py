"""Header-only OMOP output tables (no PCORnet source).

The legacy converter emits ``device_exposure``, ``visit_detail``, ``note``
and ``fact_relationship`` as empty (header-only) DataFrames — PCORnet has
no source feed for any of them (the legacy mappers build an empty
DataFrame with the OMOP schema and write it unchanged). These four
formatters reproduce that: read a guaranteed-present PCORnet source
(``DEMOGRAPHIC``) purely to obtain a live Spark plan, drop every row
(``limit(0)``), then project the fixed OMOP column list as empty string
literals.

Emitting the header-only file (rather than skipping the table) keeps the
OMOP output shape complete so downstream loaders that expect all 18 CDM
tables do not trip on a missing artefact — matching the legacy behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

    from ovid2.core.engines.protocols import EngineFunctions


def empty_with_columns(
    functions: "EngineFunctions", df: "DataFrame", columns: tuple[str, ...]
) -> "DataFrame":
    """Return a zero-row DataFrame with exactly ``columns`` (string type).

    Args:
        functions: The engine's column-expression DSL.
        df: A live source DataFrame (used only for its Spark plan).
        columns: The OMOP output column names, in order.

    Returns:
        A lazy, zero-row DataFrame carrying the requested columns.
    """
    lit = functions.lit
    return df.limit(0).select(*[lit("").alias(name) for name in columns])


class _HeaderOnlyFormatter(BaseFormatter):
    """Base for the four PCORnet-less OMOP tables."""

    #: OMOP output column list (set by each subclass).
    columns: tuple[str, ...] = ()
    #: OMOP table name (set by each subclass).
    _table: str = ""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return self._table

    @property
    def input_file_name(self) -> str:
        """Read DEMOGRAPHIC purely to obtain a live plan; all rows dropped."""
        return "DEMOGRAPHIC"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Return the header-only OMOP output (lazy, zero rows)."""
        return empty_with_columns(self.context.functions, df, self.columns)


class DeviceExposureFormatter(_HeaderOnlyFormatter):
    """Header-only OMOP device_exposure (PCORnet has no device source)."""

    _table = "device_exposure"
    columns = (
        "device_exposure_id",
        "person_id",
        "device_concept_id",
        "device_exposure_start_date",
        "device_exposure_start_datetime",
        "device_exposure_end_date",
        "device_exposure_end_datetime",
        "device_type_concept_id",
        "unique_device_id",
        "quantity",
        "provider_id",
        "visit_occurrence_id",
        "visit_detail_id",
        "device_source_value",
        "device_source_concept_id",
        "updated",
        "source",
        "mapped_from",
    )


class VisitDetailFormatter(_HeaderOnlyFormatter):
    """Header-only OMOP visit_detail (no PCORnet visit-detail source)."""

    _table = "visit_detail"
    columns = (
        "visit_detail_id",
        "person_id",
        "visit_detail_concept_id",
        "visit_detail_start_date",
        "visit_detail_start_datetime",
        "visit_detail_end_date",
        "visit_detail_end_datetime",
        "visit_detail_type_concept_id",
        "provider_id",
        "care_site_id",
        "visit_detail_source_value",
        "visit_detail_source_concept_id",
        "admitting_source_concept_id",
        "admitting_source_value",
        "discharge_to_concept_id",
        "discharge_to_source_value",
        "preceding_visit_detail_id",
        "visit_detail_parent_id",
        "visit_occurrence_id",
        "updated",
        "source",
        "mapped_from",
    )


class NoteFormatter(_HeaderOnlyFormatter):
    """Header-only OMOP note (no PCORnet clinical-note source)."""

    _table = "note"
    columns = (
        "note_id",
        "person_id",
        "note_date",
        "note_datetime",
        "note_type_concept_id",
        "note_class_concept_id",
        "note_title",
        "note_text",
        "encoding_concept_id",
        "language_concept_id",
        "provider_id",
        "visit_occurrence_id",
        "visit_detail_id",
        "note_source_value",
        "updated",
        "source",
        "mapped_from",
    )


class FactRelationshipFormatter(_HeaderOnlyFormatter):
    """Header-only OMOP fact_relationship (no PCORnet source)."""

    _table = "fact_relationship"
    columns = (
        "domain_concept_id_1",
        "fact_id_1",
        "domain_concept_id_2",
        "fact_id_2",
        "relationship_concept_id",
        "updated",
        "source",
        "mapped_from",
    )
