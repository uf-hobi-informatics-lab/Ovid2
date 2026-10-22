"""OMOP ``location`` formatter (derived from PCORnet ``ENCOUNTER``).

Ported from the legacy converter's ``02.location_from_encounter_mapper.py``.
PCORnet has no first-class location table — locations are *derived* from the
distinct ``FACILITY_LOCATION`` values on ENCOUNTER (one of the legacy
"complementary tables", previously built as
``mapping_facility_location_to_location_id.csv``). This milestone
synthesises them lazily.

Team decisions baked in:

* ``location_id`` = ``hash_key_id(FACILITY_LOCATION)`` so it matches the
  ``care_site`` formatter's ``location_id`` for the same facility location.
* The legacy converter stored ``FACILITY_LOCATION`` in the ``zip`` column
  (PCORnet ``FACILITY_LOCATION`` is a zip/geographic locator); every other
  address column is blank. ``location_source_value`` also carries it.

Rows are de-duplicated to one per ``FACILITY_LOCATION``. Fully lazy.

Output columns match ``omop_5_3.LOCATION`` (11 columns incl. the Ovid
trailing bookkeeping fields).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import hash_key_id

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class LocationFormatter(BaseFormatter):
    """Derive the OMOP location table from PCORnet ENCOUNTER facilities."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "location"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (ENCOUNTER)."""
        return "ENCOUNTER"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Derive OMOP location from distinct ENCOUNTER facilities (lazy)."""
        fns = self.context.functions
        engine = self.context.engine
        col = fns.col
        lit = fns.lit
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        distinct_locations = engine.drop_duplicates(
            df.select(
                col("facility_location"),
                col("source"),
            ),
            ["facility_location"],
        )

        return distinct_locations.select(
            hash_key_id(fns, "facility_location").alias("location_id"),
            lit("").alias("address_1"),
            lit("").alias("address_2"),
            lit("").alias("city"),
            lit("").alias("state"),
            col("facility_location").alias("zip"),
            lit("").alias("county"),
            col("facility_location").alias("location_source_value"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("ENCOUNTER").alias("mapped_from"),
        )
