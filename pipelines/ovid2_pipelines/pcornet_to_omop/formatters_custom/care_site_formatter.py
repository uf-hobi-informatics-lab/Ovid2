"""OMOP ``care_site`` formatter (derived from PCORnet ``ENCOUNTER``).

Ported from the legacy converter's ``03.care_site_mapper.py``. PCORnet has
no first-class care_site table — care sites are *derived* from the distinct
``(FACILITYID, FACILITY_TYPE, FACILITY_LOCATION)`` triples on ENCOUNTER
(one of the legacy "complementary tables"). This milestone synthesises them
lazily instead of via a precomputed ``mapping_facilityid_care_site_id.csv``.

Team decisions baked in:

* ``care_site_id`` = ``hash_key_id(FACILITYID)`` — the same deterministic
  surrogate the ``provider`` / ``location`` M2 formatters use, so the id is
  stable across runs without a mapping-table round-trip.
  ``care_site_source_value = FACILITYID`` preserves the original string.
* ``location_id`` = ``hash_key_id(FACILITY_LOCATION)`` so it lines up with
  the ``location`` formatter (which keys on the same column).
* ``place_of_service_concept_id`` from the static
  ``place_of_service_concept_id_dict`` keyed on ``FACILITY_TYPE``.

Rows are de-duplicated to one per ``FACILITYID`` (the legacy converter's
``groupBy(care_site_id).agg(max(...))`` reduction). Fully lazy.

Output columns match ``omop_5_3.CARE_SITE`` (9 columns incl. the Ovid
trailing bookkeeping fields).
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


class CareSiteFormatter(BaseFormatter):
    """Derive the OMOP care_site table from PCORnet ENCOUNTER facilities."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "care_site"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (ENCOUNTER)."""
        return "ENCOUNTER"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Derive OMOP care_site from distinct ENCOUNTER facilities (lazy)."""
        fns = self.context.functions
        engine = self.context.engine
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        facility_type = upper(trim(col("facility_type")))

        distinct_sites = engine.drop_duplicates(
            df.select(
                col("facilityid"),
                col("facility_type"),
                col("facility_location"),
                col("source"),
            ),
            ["facilityid"],
        )

        return distinct_sites.select(
            hash_key_id(fns, "facilityid").alias("care_site_id"),
            col("facilityid").alias("care_site_name"),
            literal_map(
                fns, _dicts.place_of_service_concept_id_dict, facility_type
            ).alias("place_of_service_concept_id"),
            hash_key_id(fns, "facility_location").alias("location_id"),
            col("facilityid").alias("care_site_source_value"),
            col("facility_type").alias("place_of_service_source_value"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("ENCOUNTER").alias("mapped_from"),
        )
