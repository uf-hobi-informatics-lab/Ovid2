"""OMOP ``cdm_source`` formatter (PCORnet ``HARVEST`` -> OMOP).

Ported from the legacy converter's ``39.cdm_source_mapper.py``, adapted to
the ``omop_5_3`` CDM's ``cdm_source`` shape (9 columns — the legacy
converter targeted OMOP 5.4 which carries extra ``source_description`` /
``cdm_version_concept_id`` columns that 5.3 does not).

``cdm_source`` is a single-row metadata table. The dynamic fields
(``cdm_source_name`` / ``cdm_source_abbreviation`` / ``source_release_date``)
come from the one PCORnet HARVEST row; the static holder / documentation /
version fields come from the pipeline's ``pipeline_settings`` module (with
the legacy defaults as fallback). Fully lazy — no ``.collect()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop import pipeline_settings as _settings

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

_CDM_ETL_REFERENCE = "https://ohdsi.github.io/CommonDataModel/cdm53"


class CdmSourceFormatter(BaseFormatter):
    """Map the PCORnet HARVEST row into the OMOP cdm_source metadata table."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "cdm_source"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (Snowflake table / disc dir)."""
        return "HARVEST"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape HARVEST into the single-row OMOP cdm_source table (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        to_date = fns.to_date
        current_date = fns.current_date

        holder = getattr(_settings, "CDM_HOLDER", "") or ""
        cdm_version = getattr(_settings, "CDM_VERSION", "5.3") or "5.3"
        vocab_version = getattr(_settings, "VOCABULARY_VERSION", "") or ""

        return df.select(
            col("datamart_name").alias("cdm_source_name"),
            col("datamartid").alias("cdm_source_abbreviation"),
            lit(holder).alias("cdm_holder"),
            lit("").alias("source_documentation_reference"),
            lit(_CDM_ETL_REFERENCE).alias("cdm_etl_reference"),
            to_date(col("refresh_encounter_date"), "yyyy-MM-dd").alias(
                "source_release_date"
            ),
            current_date().alias("cdm_release_date"),
            lit(cdm_version).alias("cdm_version"),
            lit(vocab_version).alias("vocabulary_version"),
        )
