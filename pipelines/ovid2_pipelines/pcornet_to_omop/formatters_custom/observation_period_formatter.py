"""OMOP ``observation_period`` formatter (PCORnet ``ENROLLMENT`` -> OMOP).

Ported from the legacy converter's ``14.observation_period_mapper.py``.

Team decisions baked in:

* ``person_id = PATID`` (no id-mapping table).
* ``observation_period_id`` is a deterministic hash of the ENROLLMENT
  natural key (``PATID`` + ``ENR_START_DATE`` + ``ENR_BASIS`` — the exact
  concat key the legacy ``mapping_enrollment_to_omop_id`` table was built
  from) via :func:`hash_key_id`, so no mapping-table round-trip is needed
  and ids are stable across runs.
* ``period_type_concept_id`` from the static ``period_type_concept_id_dict``.

Output columns match ``omop_5_3.OBSERVATION_PERIOD`` (8 columns incl. the
Ovid trailing bookkeeping fields). Fully lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import (
    default_date,
    hash_key_id,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class ObservationPeriodFormatter(BaseFormatter):
    """Map a PCORnet ENROLLMENT feed into the OMOP observation_period table."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "observation_period"

    @property
    def input_file_name(self) -> str:
        """Return the PCORnet source identifier (Snowflake table / disc dir)."""
        return "ENROLLMENT"

    def format(self, df: "DataFrame") -> "DataFrame":
        """Reshape ENROLLMENT into OMOP observation_period (lazy)."""
        fns = self.context.functions
        col = fns.col
        lit = fns.lit
        upper = fns.upper
        trim = fns.trim
        date_format = fns.date_format
        current_timestamp = fns.current_timestamp

        return df.select(
            hash_key_id(fns, "patid", "enr_start_date", "enr_basis").alias(
                "observation_period_id"
            ),
            col("patid").alias("person_id"),
            default_date(fns, "enr_start_date").alias("observation_period_start_date"),
            default_date(fns, "enr_end_date").alias("observation_period_end_date"),
            literal_map(
                fns, _dicts.period_type_concept_id_dict, upper(trim(col("enr_basis")))
            ).alias("period_type_concept_id"),
            date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss").alias("updated"),
            col("source").alias("source"),
            lit("ENROLLMENT").alias("mapped_from"),
        )
