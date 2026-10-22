"""OMOP ``person`` formatter (PCORnet ``DEMOGRAPHIC`` -> OMOP ``person``).

Ported from the legacy converter's ``00.person_mapper.py``. Team
decision: ``person_id = PATID`` (no id-mapping table). Gender / race /
ethnicity concept ids come from the static dictionaries in
``ovid2_pipelines.pcornet_to_omop.dictionaries`` (matching the legacy
``create_map`` dictionaries), applied as broadcast literal maps.

Output columns match ``omop_5_3.PERSON`` (21 columns incl. the Ovid
trailing ``updated`` / ``source`` / ``mapped_from`` bookkeeping fields).
Everything is lazy — no ``.collect()`` / ``.count()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import literal_map

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class PersonFormatter(BaseFormatter):
    """Map a PCORnet DEMOGRAPHIC feed into the OMOP person table."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "person"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (Snowflake table / disc dir)."""
        return "DEMOGRAPHIC"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape DEMOGRAPHIC into OMOP person (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        coalesce = fns.coalesce
        to_date = fns.to_date
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        # Source columns are lowercased by read_source.
        sex = upper(trim(col("sex")))
        race = upper(trim(col("race")))
        hisp = upper(trim(col("hispanic")))

        birth = coalesce(
            to_date(col("birth_date"), "yyyy-MM-dd"),
            to_date(col("birth_date"), "yyyy-MM-dd HH:mm:ss"),
        )

        return df.select(
            col("patid").alias("person_id"),
            coalesce(
                literal_map(fns, _dicts.gender_concept_id_dict, sex), lit("0")
            ).alias("gender_concept_id"),
            date_format(birth, "yyyy").alias("year_of_birth"),
            date_format(birth, "MM").alias("month_of_birth"),
            date_format(birth, "dd").alias("day_of_birth"),
            date_format(birth, "yyyy-MM-dd HH:mm:ss").alias("birth_datetime"),
            coalesce(
                literal_map(fns, _dicts.race_concept_id_dict, race), lit("0")
            ).alias("race_concept_id"),
            coalesce(
                literal_map(fns, _dicts.ethnicity_concept_id_dict, hisp), lit("0")
            ).alias("ethnicity_concept_id"),
            lit("").alias("location_id"),
            lit("").alias("provider_id"),
            lit("").alias("care_site_id"),
            col("patid").alias("person_source_value"),
            col("sex").alias("gender_source_value"),
            coalesce(
                literal_map(fns, _dicts.gender_source_concept_id_dict, sex),
                lit("44814653"),
            ).alias("gender_source_concept_id"),
            col("race").alias("race_source_value"),
            coalesce(
                literal_map(fns, _dicts.race_source_concept_id_dict, race),
                lit("44814653"),
            ).alias("race_source_concept_id"),
            col("hispanic").alias("ethnicity_source_value"),
            coalesce(
                literal_map(fns, _dicts.ethnicity_source_concept_id_dict, hisp),
                lit("44814653"),
            ).alias("ethnicity_source_concept_id"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("DEMOGRAPHIC").alias("mapped_from"),
        )
