"""OMOP ``drug_exposure`` formatter (four PCORnet drug sources -> OMOP).

Ported from the legacy converter's ``05.drug_exposure_mapper.py``. Fans in
PCORnet ``MED_ADMIN`` + ``PRESCRIBING`` + ``DISPENSING`` + ``IMMUNIZATION``
into OMOP ``drug_exposure`` (domain = Drug), collapsed into one direct
formatter (no persisted ``common`` staging table).

Team decisions: ``drug_exposure_id`` = the PCORnet surrogate id
(MEDADMINID / PRESCRIBINGID / DISPENSINGID / IMMUNIZATIONID),
``person_id`` = PATID, ``visit_occurrence_id`` = ENCOUNTERID,
``provider_id`` = the source's provider column. Concept ids resolved live
against the ``concept`` / ``concept_relationship`` shared views (domain =
Drug); only rows whose code resolves to a valid Drug-domain source concept
survive (the legacy inner ``mapping_drug_source_concept_id`` join). Fully
lazy.

The four PCORnet sources are all optional siblings: a pipeline that ships
only a subset contributes only those legs.
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
    default_datetime_with_time,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import Column, DataFrame

    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineFunctions

_DOMAIN = "Drug"


class DrugExposureFormatter(BaseFormatter):
    """Map the four PCORnet drug sources into OMOP drug_exposure."""

    @property
    def table_name(self) -> str:
        """Return the OMOP output table name."""
        return "drug_exposure"

    @property
    def input_file_name(self) -> str:
        """Primary source (MED_ADMIN); the rest read as siblings."""
        return "MED_ADMIN"

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
        """Fan-in + Drug-domain resolution + project (lazy)."""
        fns = self.context.functions
        engine = self.context.engine
        col = fns.col

        legs: list[DataFrame] = [_med_admin(df, fns)]
        for identifier, builder in (
            ("PRESCRIBING", _prescribing),
            ("DISPENSING", _dispensing),
            ("IMMUNIZATION", _immunization),
        ):
            try:
                raw = self._read_sibling(identifier)
            except FileNotFoundError:
                continue
            legs.append(builder(raw, fns))

        rows = legs[0]
        for leg in legs[1:]:
            rows = engine.union_by_name(rows, leg, allow_missing_columns=True)

        resolved = resolve_concepts(
            rows,
            self.context.spark,
            functions=fns,
            engine=engine,
            domain=_DOMAIN,
            prefix="drug",
            code_column="drug_code",
            code_type_column="drug_code_type",
            require_source_concept=True,
        )

        return resolved.select(
            col("drug_exposure_id").alias("drug_exposure_id"),
            col("person_id").alias("person_id"),
            col("drug_concept_id").alias("drug_concept_id"),
            col("start_date_src").alias("drug_exposure_start_date"),
            col("start_datetime").alias("drug_exposure_start_datetime"),
            col("end_date_src").alias("drug_exposure_end_date"),
            col("end_datetime").alias("drug_exposure_end_datetime"),
            col("end_date_src").alias("verbatim_end_date"),
            col("drug_type_concept_id").alias("drug_type_concept_id"),
            col("stop_reason").alias("stop_reason"),
            col("refills").alias("refills"),
            col("quantity").alias("quantity"),
            col("days_supply").alias("days_supply"),
            col("sig").alias("sig"),
            col("route_concept_id").alias("route_concept_id"),
            col("lot_number").alias("lot_number"),
            col("provider_id").alias("provider_id"),
            col("visit_occurrence_id").alias("visit_occurrence_id"),
            fns.lit("").alias("visit_detail_id"),
            col("drug_code").alias("drug_source_value"),
            col("drug_source_concept_id").alias("drug_source_concept_id"),
            col("route_source_value").alias("route_source_value"),
            col("dose_unit_source_value").alias("dose_unit_source_value"),
            col("updated").alias("updated"),
            col("source").alias("source"),
            col("mapped_from").alias("mapped_from"),
        )


def _updated(functions: "EngineFunctions") -> "Column":
    return functions.date_format(functions.current_timestamp(), "yyyy-MM-dd HH:mm:ss")


def _route_map(functions: "EngineFunctions", key_col: "Column") -> "Column":
    return literal_map(functions, _dicts.route_concept_id_dict, key_col)


def _mask_ni(functions: "EngineFunctions", column: "Column") -> "Column":
    up = functions.upper(column)
    return functions.when(up.isin("OT", "UN"), functions.lit("NI")).otherwise(up)


def _med_admin(df: "DataFrame", functions: "EngineFunctions") -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    upper = fns.upper

    return df.select(
        col("medadminid").alias("drug_exposure_id"),
        col("patid").alias("person_id"),
        col("medadmin_code").alias("drug_code"),
        _mask_ni(fns, col("medadmin_type")).alias("drug_code_type"),
        default_date(fns, "medadmin_start_date").alias("start_date_src"),
        default_datetime_with_time(fns, "medadmin_start_date", "medadmin_start_time").alias(
            "start_datetime"
        ),
        default_date(fns, "medadmin_stop_date").alias("end_date_src"),
        default_datetime_with_time(fns, "medadmin_stop_date", "medadmin_stop_time").alias(
            "end_datetime"
        ),
        lit("32830").alias("drug_type_concept_id"),
        lit("").alias("stop_reason"),
        lit("").alias("refills"),
        lit("").alias("quantity"),
        col("medadmin_dose_admin").alias("days_supply"),
        lit("").alias("sig"),
        _route_map(fns, upper(col("medadmin_route"))).alias("route_concept_id"),
        lit("").alias("lot_number"),
        col("medadmin_providerid").alias("provider_id"),
        col("encounterid").alias("visit_occurrence_id"),
        col("medadmin_route").alias("route_source_value"),
        col("medadmin_dose_admin_unit").alias("dose_unit_source_value"),
        _updated(fns).alias("updated"),
        col("source").alias("source"),
        lit("MED_ADMIN").alias("mapped_from"),
    )


def _prescribing(df: "DataFrame", functions: "EngineFunctions") -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    upper = fns.upper
    when = fns.when
    concat = fns.concat

    # RX_FREQUENCY -> SIG text, ported from the legacy inline
    # ``prescribing_sig`` UDF as a CASE chain over RAW_RX_MED_NAME.
    freq = upper(col("rx_frequency"))
    med = col("raw_rx_med_name")
    sig = (
        when(freq == "01", concat(med, lit(" Every day")))
        .when(freq == "02", concat(med, lit(" Two times a day (BID)")))
        .when(freq == "03", concat(med, lit(" Three times a day (TID)")))
        .when(freq == "04", concat(med, lit(" Four times a day (QID)")))
        .when(freq == "05", concat(med, lit(" Every morning")))
        .when(freq == "06", concat(med, lit(" Every afternoon")))
        .when(freq == "07", concat(med, lit(" Before meals")))
        .when(freq == "08", concat(med, lit(" After meals")))
        .when(freq == "10", concat(med, lit(" Every evening")))
        .when(freq == "11", concat(med, lit(" Once")))
        .when(freq == "UN", lit("Unknown"))
        .when(freq == "OT", lit("Other"))
        .otherwise(lit("No information"))
    )

    return df.select(
        col("prescribingid").alias("drug_exposure_id"),
        col("patid").alias("person_id"),
        col("rxnorm_cui").alias("drug_code"),
        lit("RX").alias("drug_code_type"),
        default_date(fns, "rx_start_date").alias("start_date_src"),
        default_datetime(fns, "rx_start_date").alias("start_datetime"),
        default_date(fns, "rx_end_date").alias("end_date_src"),
        default_datetime(fns, "rx_end_date").alias("end_datetime"),
        lit("32833").alias("drug_type_concept_id"),
        lit("").alias("stop_reason"),
        col("rx_refills").alias("refills"),
        col("rx_quantity").alias("quantity"),
        col("rx_days_supply").alias("days_supply"),
        sig.alias("sig"),
        _route_map(fns, upper(col("rx_route"))).alias("route_concept_id"),
        lit("").alias("lot_number"),
        col("rx_providerid").alias("provider_id"),
        col("encounterid").alias("visit_occurrence_id"),
        col("rx_route").alias("route_source_value"),
        col("rx_dose_ordered_unit").alias("dose_unit_source_value"),
        _updated(fns).alias("updated"),
        col("source").alias("source"),
        lit("PRESCRIBING").alias("mapped_from"),
    )


def _dispensing(df: "DataFrame", functions: "EngineFunctions") -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    upper = fns.upper

    return df.select(
        col("dispensingid").alias("drug_exposure_id"),
        col("patid").alias("person_id"),
        col("ndc").alias("drug_code"),
        lit("ND").alias("drug_code_type"),
        default_date(fns, "dispense_date").alias("start_date_src"),
        default_datetime(fns, "dispense_date").alias("start_datetime"),
        default_date(fns, "dispense_date").alias("end_date_src"),
        default_datetime(fns, "dispense_date").alias("end_datetime"),
        lit("32825").alias("drug_type_concept_id"),
        lit("").alias("stop_reason"),
        lit("").alias("refills"),
        col("dispense_amt").alias("quantity"),
        col("dispense_sup").alias("days_supply"),
        lit("").alias("sig"),
        _route_map(fns, upper(col("dispense_route"))).alias("route_concept_id"),
        lit("").alias("lot_number"),
        lit("").alias("provider_id"),
        lit("").alias("visit_occurrence_id"),
        col("dispense_route").alias("route_source_value"),
        col("raw_dispense_dose_disp_unit").alias("dose_unit_source_value"),
        _updated(fns).alias("updated"),
        col("source").alias("source"),
        lit("DISPENSING").alias("mapped_from"),
    )


def _immunization(df: "DataFrame", functions: "EngineFunctions") -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    upper = fns.upper

    return df.select(
        col("immunizationid").alias("drug_exposure_id"),
        col("patid").alias("person_id"),
        col("vx_code").alias("drug_code"),
        _mask_ni(fns, col("vx_code_type")).alias("drug_code_type"),
        default_date(fns, "vx_admin_date").alias("start_date_src"),
        default_datetime(fns, "vx_admin_date").alias("start_datetime"),
        default_date(fns, "vx_admin_date").alias("end_date_src"),
        default_datetime(fns, "vx_admin_date").alias("end_datetime"),
        literal_map(
            fns, _dicts.immunization_type_concept_id_dict, upper(col("vx_source"))
        ).alias("drug_type_concept_id"),
        lit("").alias("stop_reason"),
        lit("").alias("refills"),
        col("vx_dose").alias("quantity"),
        lit("").alias("days_supply"),
        lit("").alias("sig"),
        _route_map(fns, upper(col("vx_route"))).alias("route_concept_id"),
        col("vx_lot_num").alias("lot_number"),
        col("vx_providerid").alias("provider_id"),
        col("encounterid").alias("visit_occurrence_id"),
        col("vx_route").alias("route_source_value"),
        col("vx_dose_unit").alias("dose_unit_source_value"),
        _updated(fns).alias("updated"),
        col("source").alias("source"),
        lit("IMMUNIZATION").alias("mapped_from"),
    )
