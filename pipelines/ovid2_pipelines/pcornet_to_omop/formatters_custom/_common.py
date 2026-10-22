"""Shared "common" fan-in for the coded-clinical OMOP domains.

The legacy converter routed PCORnet ``LAB_RESULT_CM`` / ``PROCEDURES`` /
``OBS_CLIN`` / ``OBS_GEN`` / ``VITAL`` through an intermediate ``common``
staging table (the ``06/07/10/11/12.*_to_common`` mappers), then split
that ``common`` table into ``measurement`` / ``procedure_occurrence`` /
``observation`` by domain (the ``16/17/18/19.from_common_to_*`` mappers,
whose ``filter_common_table`` inner-join against the domain concept map is
the domain discriminator).

Ovid2 does not persist ``common``. This module reproduces the fan-in as a
single lazy DataFrame — :func:`build_common` unions every available source
into one shared 18-column schema — which the three domain formatters then
resolve + filter via :func:`_concepts.resolve_concepts` (inner join =
"only rows whose code exists in this domain survive"), exactly as the
legacy split did.

Every source is optional: a pipeline that ships no ``VITAL`` feed simply
contributes no VITAL rows. All reads go through
:func:`ovid2.formatters.adapters._sources.read_source`; everything is
lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.adapters._sources import read_source
from ovid2.formatters.spec import SourceSpec

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import (
    default_date,
    default_datetime,
    default_datetime_with_time,
    default_time,
    literal_map,
)

if TYPE_CHECKING:
    from pyspark.sql import Column, DataFrame

    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineFunctions
    from ovid2.formatters.adapters.base import AdapterContext

#: Column order of the shared "common" DataFrame. Every source leg selects
#: exactly these columns (in this order) so ``unionByName`` is trivially
#: aligned.
COMMON_COLUMNS: tuple[str, ...] = (
    "common_id",
    "person_id",
    "visit_occurrence_id",
    "visit_detail_id",
    "provider_id",
    "event_start_date",
    "event_start_datetime",
    "event_start_time",
    "common_data_type_concept_id",
    "common_data_source_value",
    "pcornet_code_type",
    "operator_concept_id",
    "value_as_number",
    "value_as_concept_id",
    "unit_concept_id",
    "range_low",
    "range_high",
    "unit_source_value",
    "value_source_value",
    "value_as_string",
    "qualifier_concept_id",
    "qualifier_source_value",
    "quantity",
    "updated",
    "source",
    "mapped_from",
)


def _mask_ot_un(functions: "EngineFunctions", column: "Column") -> "Column":
    """Rewrite PCORnet ``OT`` / ``UN`` code-types to ``NI`` (legacy masking)."""
    up = functions.upper(column)
    return functions.when(up.isin("OT", "UN"), functions.lit("NI")).otherwise(up)


def _read(ctx: "AdapterContext", identifier: str) -> "DataFrame":
    """Read one PCORnet source by identifier (lowercased columns)."""
    return read_source(
        SourceSpec(name=identifier, glob=identifier, lowercase_columns=True),
        spark=ctx.spark,
        input_address=ctx.input_address,
        pipeline=ctx.pipeline,
        batch_id=ctx.batch_id,
        engine=ctx.engine,
        input_address_name=ctx.input_address_name,
    )


def build_common(
    primary: "DataFrame", ctx: "AdapterContext", *, table_name: str = "common"
) -> "DataFrame":
    """Return the unified lazy "common" DataFrame across all coded sources.

    Args:
        primary: The DataFrame the pipeline already read for the
            formatter's ``input_file_name`` (LAB_RESULT_CM). Reused so we
            do not re-read it.
        ctx: Adapter context (for sibling reads + Spark session).
        table_name: The OMOP output table being built (``measurement`` /
            ``observation`` / ``procedure_occurrence``). Used to scope the
            checkpoint's transient-table name so concurrent domain
            formatters do not collide within a shared session.

    Returns:
        A DataFrame with exactly :data:`COMMON_COLUMNS`, the union of
        every available source leg. Sources that are absent are skipped.

        The fan-in is **checkpointed** through ``engine.checkpoint`` so
        the 5-source union (LAB ×2 legs + PROCEDURES + OBS_CLIN + OBS_GEN
        + the 8-way VITAL fan-out) is computed exactly once instead of
        being re-derived inside each domain formatter's final write. On
        Spark this is a ``.cache()`` boundary; on Snowpark it is a
        transient table the concept-resolution join then scans. Without
        this, the ``measurement`` CTAS re-executes the whole multi-TB
        fan-in graph in a single statement (the root cause of the
        16-hour measurement runs).
    """
    legs: list[DataFrame] = []

    functions = ctx.functions
    engine = ctx.engine

    lab = _lab_result_cm(primary, functions, engine)
    if lab is not None:
        legs.append(lab)

    for reader in (_procedures, _obs_clin, _obs_gen, _vital):
        try:
            raw = _read(ctx, reader.identifier)  # type: ignore[attr-defined]
        except FileNotFoundError:
            continue
        legs.append(reader(raw, functions, engine))

    combined = legs[0]
    for leg in legs[1:]:
        combined = engine.union_by_name(combined, leg, allow_missing_columns=True)

    # Checkpoint the fan-in so it is materialised once. The logical name
    # is scoped to the table being built so parallel domain formatters
    # (measurement / observation / procedure_occurrence) do not collide on
    # the transient-table name within a shared session.
    return engine.checkpoint(combined, f"common_{table_name}")


# --- LAB_RESULT_CM leg (primary; Measurement/Procedure via LAB_PX) ----------


def _lab_result_cm(
    df: "DataFrame", functions: "EngineFunctions", engine: "BaseEngine"
) -> "DataFrame | None":
    """Two rows per lab: the LOINC result (Measurement) + LAB_PX (Procedure)."""
    fns = functions
    col = fns.col
    lit = fns.lit
    concat = fns.concat
    trim = fns.trim
    upper = fns.upper

    updated = fns.date_format(fns.current_timestamp(), "yyyy-MM-dd HH:mm:ss")

    result_leg = df.select(
        col("lab_result_cm_id").alias("common_id"),
        col("patid").alias("person_id"),
        col("encounterid").alias("visit_occurrence_id"),
        lit("").alias("visit_detail_id"),
        lit("").alias("provider_id"),
        default_date(fns, "result_date").alias("event_start_date"),
        default_datetime_with_time(fns, "result_date", "result_time").alias(
            "event_start_datetime"
        ),
        default_time(fns, "result_time").alias("event_start_time"),
        literal_map(
            fns,
            _dicts.lab_result_source_to_type_concept_id_dict,
            upper(trim(col("lab_result_source"))),
        ).alias("common_data_type_concept_id"),
        col("lab_loinc").alias("common_data_source_value"),
        lit("LC").alias("pcornet_code_type"),
        literal_map(
            fns, _dicts.operator_concept_id_dict, upper(trim(col("result_modifier")))
        ).alias("operator_concept_id"),
        col("result_num").alias("value_as_number"),
        literal_map(
            fns,
            _dicts.result_qual_to_value_as_concept_id_dict,
            upper(trim(col("result_qual"))),
        ).alias("value_as_concept_id"),
        literal_map(
            fns,
            _dicts.result_unit_to_unit_concept_id_dict,
            upper(trim(col("result_unit"))),
        ).alias("unit_concept_id"),
        col("norm_range_low").alias("range_low"),
        col("norm_range_high").alias("range_high"),
        col("result_unit").alias("unit_source_value"),
        concat(col("result_num"), lit(" - "), col("result_qual")).alias(
            "value_source_value"
        ),
        lit("").alias("value_as_string"),
        lit("").alias("qualifier_concept_id"),
        lit("").alias("qualifier_source_value"),
        lit("").alias("quantity"),
        updated.alias("updated"),
        col("source").alias("source"),
        lit("LAB_RESULT_CM").alias("mapped_from"),
    )

    px_leg = df.filter(
        col("lab_px").isNotNull() & (trim(col("lab_px").cast("string")) != "")
    ).select(
        col("lab_result_cm_id").alias("common_id"),
        col("patid").alias("person_id"),
        col("encounterid").alias("visit_occurrence_id"),
        lit("").alias("visit_detail_id"),
        lit("").alias("provider_id"),
        default_date(fns, "lab_order_date").alias("event_start_date"),
        default_datetime(fns, "lab_order_date").alias("event_start_datetime"),
        lit("").alias("event_start_time"),
        literal_map(
            fns,
            _dicts.lab_result_source_to_type_concept_id_dict,
            upper(trim(col("lab_result_source"))),
        ).alias("common_data_type_concept_id"),
        col("lab_px").alias("common_data_source_value"),
        lit("NI").alias("pcornet_code_type"),
        literal_map(
            fns, _dicts.operator_concept_id_dict, upper(trim(col("result_modifier")))
        ).alias("operator_concept_id"),
        lit("").alias("value_as_number"),
        lit("").alias("value_as_concept_id"),
        lit("").alias("unit_concept_id"),
        lit("").alias("range_low"),
        lit("").alias("range_high"),
        lit("").alias("unit_source_value"),
        lit("").alias("value_source_value"),
        lit("").alias("value_as_string"),
        lit("44814650").alias("qualifier_concept_id"),
        lit("No Information").alias("qualifier_source_value"),
        lit("").alias("quantity"),
        updated.alias("updated"),
        col("source").alias("source"),
        lit("LAB_RESULT_CM_PX").alias("mapped_from"),
    )
    return engine.union_by_name(result_leg, px_leg)


# --- PROCEDURES leg (Procedure domain) --------------------------------------


def _procedures(
    df: "DataFrame", functions: "EngineFunctions", engine: "BaseEngine"
) -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    upper = fns.upper
    when = fns.when

    updated = fns.date_format(fns.current_timestamp(), "yyyy-MM-dd HH:mm:ss")
    px_type = _mask_ot_un(fns, col("px_type"))

    # PX_SOURCE (+ PPX) -> procedure type concept id, ported from the
    # legacy inline ``map_procedure_type_concept_id`` UDF as a CASE chain.
    px_source = upper(col("px_source"))
    ppx = upper(col("ppx"))
    type_concept = (
        when(px_source == "OD", lit("32817"))
        .when((px_source == "BI") & (ppx == "P"), lit("44786630"))
        .when((px_source == "BI") & (ppx == "S"), lit("44786631"))
        .when(px_source == "BI", lit("32821"))
        .when(px_source == "CL", lit("32810"))
        .when(px_source == "DR", lit("45754907"))
        .otherwise(lit("0"))
    )

    return df.select(
        col("proceduresid").alias("common_id"),
        col("patid").alias("person_id"),
        col("encounterid").alias("visit_occurrence_id"),
        lit("").alias("visit_detail_id"),
        col("providerid").alias("provider_id"),
        default_date(fns, "px_date").alias("event_start_date"),
        default_datetime(fns, "px_date").alias("event_start_datetime"),
        lit("").alias("event_start_time"),
        type_concept.alias("common_data_type_concept_id"),
        col("px").alias("common_data_source_value"),
        px_type.alias("pcornet_code_type"),
        lit("").alias("operator_concept_id"),
        lit("").alias("value_as_number"),
        lit("").alias("value_as_concept_id"),
        lit("").alias("unit_concept_id"),
        lit("").alias("range_low"),
        lit("").alias("range_high"),
        lit("").alias("unit_source_value"),
        lit("").alias("value_source_value"),
        lit("").alias("value_as_string"),
        lit("").alias("qualifier_concept_id"),
        lit("").alias("qualifier_source_value"),
        lit("").alias("quantity"),
        updated.alias("updated"),
        col("source").alias("source"),
        lit("PROCEDURES").alias("mapped_from"),
    )


_procedures.identifier = "PROCEDURES"  # type: ignore[attr-defined]


# --- OBS_CLIN leg -----------------------------------------------------------


def _obs_clin(
    df: "DataFrame", functions: "EngineFunctions", engine: "BaseEngine"
) -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    trim = fns.trim
    upper = fns.upper

    updated = fns.date_format(fns.current_timestamp(), "yyyy-MM-dd HH:mm:ss")
    code_type = _mask_ot_un(fns, col("obsclin_type"))

    return df.select(
        col("obsclinid").alias("common_id"),
        col("patid").alias("person_id"),
        col("encounterid").alias("visit_occurrence_id"),
        lit("").alias("visit_detail_id"),
        col("obsclin_providerid").alias("provider_id"),
        default_date(fns, "obsclin_start_date").alias("event_start_date"),
        default_datetime_with_time(fns, "obsclin_start_date", "obsclin_start_time").alias(
            "event_start_datetime"
        ),
        default_time(fns, "obsclin_start_time").alias("event_start_time"),
        literal_map(
            fns,
            _dicts.obs_clin_type_concept_id_dict,
            upper(trim(col("obsclin_source"))),
        ).alias("common_data_type_concept_id"),
        col("obsclin_code").alias("common_data_source_value"),
        code_type.alias("pcornet_code_type"),
        lit("").alias("operator_concept_id"),
        col("obsclin_result_num").alias("value_as_number"),
        literal_map(
            fns,
            _dicts.obs_clin_result_qual_value_as_concept_id_dict,
            upper(trim(col("obsclin_result_qual"))),
        ).alias("value_as_concept_id"),
        literal_map(
            fns,
            _dicts.result_unit_to_unit_concept_id_dict,
            upper(trim(col("obsclin_result_unit"))),
        ).alias("unit_concept_id"),
        lit("").alias("range_low"),
        lit("").alias("range_high"),
        col("obsclin_result_unit").alias("unit_source_value"),
        lit("").alias("value_source_value"),
        col("obsclin_result_text").alias("value_as_string"),
        literal_map(
            fns,
            _dicts.obs_clin_result_modifier_qualifier_concept_id_dict,
            upper(trim(col("obsclin_result_modifier"))),
        ).alias("qualifier_concept_id"),
        col("obsclin_result_modifier").alias("qualifier_source_value"),
        lit("").alias("quantity"),
        updated.alias("updated"),
        col("source").alias("source"),
        lit("OBS_CLIN").alias("mapped_from"),
    )


_obs_clin.identifier = "OBS_CLIN"  # type: ignore[attr-defined]


# --- OBS_GEN leg ------------------------------------------------------------


def _obs_gen(
    df: "DataFrame", functions: "EngineFunctions", engine: "BaseEngine"
) -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    trim = fns.trim
    upper = fns.upper

    updated = fns.date_format(fns.current_timestamp(), "yyyy-MM-dd HH:mm:ss")
    code_type = _mask_ot_un(fns, col("obsgen_type"))

    return df.select(
        col("obsgenid").alias("common_id"),
        col("patid").alias("person_id"),
        col("encounterid").alias("visit_occurrence_id"),
        lit("").alias("visit_detail_id"),
        col("obsgen_providerid").alias("provider_id"),
        default_date(fns, "obsgen_start_date").alias("event_start_date"),
        default_datetime_with_time(fns, "obsgen_start_date", "obsgen_start_time").alias(
            "event_start_datetime"
        ),
        default_time(fns, "obsgen_start_time").alias("event_start_time"),
        literal_map(
            fns, _dicts.obs_gen_type_concept_id_dict, upper(trim(col("obsgen_source")))
        ).alias("common_data_type_concept_id"),
        col("obsgen_code").alias("common_data_source_value"),
        code_type.alias("pcornet_code_type"),
        lit("").alias("operator_concept_id"),
        col("obsgen_result_num").alias("value_as_number"),
        literal_map(
            fns,
            _dicts.obs_gen_result_qual_value_as_concept_id_dict,
            upper(trim(col("obsgen_result_qual"))),
        ).alias("value_as_concept_id"),
        literal_map(
            fns,
            _dicts.result_unit_to_unit_concept_id_dict,
            upper(trim(col("obsgen_result_unit"))),
        ).alias("unit_concept_id"),
        lit("").alias("range_low"),
        lit("").alias("range_high"),
        col("obsgen_result_unit").alias("unit_source_value"),
        lit("").alias("value_source_value"),
        col("obsgen_result_text").alias("value_as_string"),
        literal_map(
            fns,
            _dicts.obs_gen_result_modifier_qualifier_concept_id_dict,
            upper(trim(col("obsgen_result_modifier"))),
        ).alias("qualifier_concept_id"),
        col("obsgen_result_modifier").alias("qualifier_source_value"),
        lit("").alias("quantity"),
        updated.alias("updated"),
        col("source").alias("source"),
        lit("OBS_GEN").alias("mapped_from"),
    )


_obs_gen.identifier = "OBS_GEN"  # type: ignore[attr-defined]


# --- VITAL leg (fan-out into up to 8 measure rows) --------------------------


def _vital(
    df: "DataFrame", functions: "EngineFunctions", engine: "BaseEngine"
) -> "DataFrame":
    fns = functions
    col = fns.col
    lit = fns.lit
    trim = fns.trim
    upper = fns.upper

    updated = fns.date_format(fns.current_timestamp(), "yyyy-MM-dd HH:mm:ss")

    bp_pos = upper(trim(col("bp_position")))
    type_concept = literal_map(
        fns, _dicts.vital_source_to_type_concept_id_dict, upper(trim(col("vital_source")))
    )

    def _base(measure: str, mapped_from: str) -> dict[str, "Column"]:
        """Columns shared by every VITAL branch (id keyed on VITALID + measure)."""
        return {
            "common_id": fns.concat_ws("_", col("vitalid"), lit(measure)).alias(
                "common_id"
            ),
            "person_id": col("patid").alias("person_id"),
            "visit_occurrence_id": col("encounterid").alias("visit_occurrence_id"),
            "visit_detail_id": lit("").alias("visit_detail_id"),
            "provider_id": lit("").alias("provider_id"),
            "event_start_date": default_date(fns, "measure_date").alias(
                "event_start_date"
            ),
            "event_start_datetime": default_datetime_with_time(
                fns, "measure_date", "measure_time"
            ).alias("event_start_datetime"),
            "event_start_time": default_time(fns, "measure_time").alias(
                "event_start_time"
            ),
            "common_data_type_concept_id": type_concept.alias(
                "common_data_type_concept_id"
            ),
            "range_low": lit("").alias("range_low"),
            "range_high": lit("").alias("range_high"),
            "quantity": lit("").alias("quantity"),
            "updated": updated.alias("updated"),
            "source": col("source").alias("source"),
            "mapped_from": lit(mapped_from).alias("mapped_from"),
        }

    def _numeric_branch(
        measure: str,
        mapped_from: str,
        value_col: str,
        source_value: "Column",
        unit_cid: str,
        unit_src: str,
        code_type: "Column",
    ) -> "DataFrame":
        b = _base(measure, mapped_from)
        rows = df.filter(
            (col(value_col).isNotNull()) & (trim(col(value_col).cast("string")) != "")
        )
        return rows.select(
            b["common_id"], b["person_id"], b["visit_occurrence_id"],
            b["visit_detail_id"], b["provider_id"], b["event_start_date"],
            b["event_start_datetime"], b["event_start_time"],
            b["common_data_type_concept_id"],
            source_value.alias("common_data_source_value"),
            code_type.alias("pcornet_code_type"),
            lit("4172703").alias("operator_concept_id"),
            col(value_col).alias("value_as_number"),
            lit("").alias("value_as_concept_id"),
            lit(unit_cid).alias("unit_concept_id"),
            b["range_low"], b["range_high"],
            lit(unit_src).alias("unit_source_value"),
            col(value_col).alias("value_source_value"),
            lit("").alias("value_as_string"),
            lit("").alias("qualifier_concept_id"),
            lit("").alias("qualifier_source_value"),
            b["quantity"], b["updated"], b["source"], b["mapped_from"],
        )

    def _coded_branch(
        measure: str,
        mapped_from: str,
        value_col: str,
        source_code: "Column",
        code_type: "Column",
        cid_map: dict[str, str],
        txt_map: dict[str, str],
    ) -> "DataFrame":
        b = _base(measure, mapped_from)
        code = upper(trim(col(value_col)))
        rows = df.filter(
            (col(value_col).isNotNull())
            & (~code.isin("", "OT", "NI", "UN"))
        )
        return rows.select(
            b["common_id"], b["person_id"], b["visit_occurrence_id"],
            b["visit_detail_id"], b["provider_id"], b["event_start_date"],
            b["event_start_datetime"], b["event_start_time"],
            b["common_data_type_concept_id"],
            source_code.alias("common_data_source_value"),
            code_type.alias("pcornet_code_type"),
            lit("").alias("operator_concept_id"),
            lit("").alias("value_as_number"),
            literal_map(fns, cid_map, code).alias("value_as_concept_id"),
            lit("").alias("unit_concept_id"),
            b["range_low"], b["range_high"],
            lit("").alias("unit_source_value"),
            col(value_col).alias("value_source_value"),
            literal_map(fns, txt_map, code).alias("value_as_string"),
            lit("").alias("qualifier_concept_id"),
            lit("").alias("qualifier_source_value"),
            b["quantity"], b["updated"], b["source"], b["mapped_from"],
        )

    branches = [
        _numeric_branch("WT", "VITAL_WT", "wt", lit("29463-7"), "8739", "[lb_us]", lit("LC")),
        _numeric_branch("HT", "VITAL_HT", "ht", lit("8302-2"), "9330", "inches", lit("LC")),
        _numeric_branch("BMI", "VITAL_BMI", "original_bmi", lit("39156-5"), "9531", "kg/m2", lit("LC")),
        _numeric_branch("DIASTOLIC", "VITAL_DIASTOLIC", "diastolic", literal_map(fns, _dicts.bp_position_diastolic_to_pb_code_dict, bp_pos), "8876", "mmHg", literal_map(fns, _dicts.bp_position_to_pb_code_type_dict, bp_pos)),
        _numeric_branch("SYSTOLIC", "VITAL_SYSTOLIC", "systolic", literal_map(fns, _dicts.bp_position_systolic_to_pb_code_dict, bp_pos), "8876", "mmHg", literal_map(fns, _dicts.bp_position_to_pb_code_type_dict, bp_pos)),
        _coded_branch("SMOKING", "VITAL_SMOKING", "smoking", lit("63638-1"), lit("LC"), _dicts.smoking_to_smoking_concept_id_dict, _dicts.smoking_to_smoking_text_dict),
        _coded_branch("TOBACCO", "VITAL_TOBACCO", "tobacco", lit("229819007"), lit("SM"), _dicts.tobacco_to_tobacco_concept_id_dict, _dicts.tobacco_to_tobacco_text_dict),
        _coded_branch("TOBACCO_TYPE", "VITAL_TOBACCO_TYPE", "tobacco_type", lit("229819007"), lit("SM"), _dicts.tobacco_type_to_tobacco_type_concept_id_dict, _dicts.tobacco_type_to_tobacco_type_text_dict),
    ]
    combined = branches[0]
    for branch in branches[1:]:
        combined = engine.union_by_name(combined, branch)
    return combined


_vital.identifier = "VITAL"  # type: ignore[attr-defined]
