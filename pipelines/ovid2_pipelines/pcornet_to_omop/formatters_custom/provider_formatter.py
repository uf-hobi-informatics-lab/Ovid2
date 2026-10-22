"""OMOP ``provider`` formatter (PCORnet ``PROVIDER`` -> OMOP).

Ported from the legacy converter's ``01.provider_mapper.py``.

Team decisions baked in:

* ``provider_id`` is a deterministic hash of ``PROVIDERID`` via
  :func:`hash_key_id` (the legacy converter used a
  ``mapping_providerid_provider_id`` table to turn the string PROVIDERID
  into a numeric OMOP id; the hash reproduces a stable numeric id without
  the mapping-table round-trip). ``provider_source_value = PROVIDERID``
  preserves the original string.
* ``gender_concept_id`` / ``gender_source_concept_id`` from the static
  gender dictionaries (shared with the M1 ``person`` formatter).
* ``specialty_concept_id`` / ``specialty_source_concept_id`` are left at
  ``'0'`` in M2 — the legacy converter resolved provider specialty via a
  precomputed NUCC-taxonomy mapping table
  (``mapping_provider_specialty_*_concept_id``) that is out of scope for
  this milestone. ``specialty_source_value`` still carries the raw
  ``PROVIDER_SPECIALTY_PRIMARY`` so a later milestone can resolve it.

Output columns match ``omop_5_3.PROVIDER`` (16 columns incl. the Ovid
trailing bookkeeping fields). Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import (
    hash_key_id,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class ProviderFormatter(BaseFormatter):
    """Map a PCORnet PROVIDER feed into the OMOP provider table."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "provider"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (Snowflake table / disc dir)."""
        return "PROVIDER"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape PROVIDER into OMOP provider (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        sex = upper(trim(col("provider_sex")))

        return df.select(
            hash_key_id(fns, "providerid").alias("provider_id"),
            lit("").alias("provider_name"),
            col("provider_npi").alias("npi"),
            lit("").alias("dea"),
            lit("").alias("care_site_id"),
            lit("").alias("year_of_birth"),
            lit("0").alias("specialty_concept_id"),
            literal_map(fns, _dicts.gender_concept_id_dict, sex).alias(
                "gender_concept_id"
            ),
            col("providerid").alias("provider_source_value"),
            col("provider_specialty_primary").alias("specialty_source_value"),
            lit("0").alias("specialty_source_concept_id"),
            col("provider_sex").alias("gender_source_value"),
            literal_map(fns, _dicts.gender_source_concept_id_dict, sex).alias(
                "gender_source_concept_id"
            ),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("PROVIDER").alias("mapped_from"),
        )
