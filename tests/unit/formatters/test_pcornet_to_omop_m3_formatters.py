"""Execution unit tests for the M3 PCORnet -> OMOP converter formatters.

Covers the remaining clinical tier added in M3:

* ``measurement`` / ``procedure_occurrence`` / ``observation`` — the
  coded-clinical fan-in (LAB_RESULT_CM + PROCEDURES + OBS_CLIN + OBS_GEN +
  VITAL) routed to a single OMOP domain via the concept views (the legacy
  ``*_to_common`` + ``from_common_to_*`` split).
* ``drug_exposure`` — the four PCORnet drug sources (MED_ADMIN +
  PRESCRIBING + DISPENSING + IMMUNIZATION).
* ``specimen`` — PCORnet LAB_RESULT_CM specimen fields.
* ``device_exposure`` / ``visit_detail`` / ``note`` /
  ``fact_relationship`` — header-only (PCORnet has no source feed).

Each formatter is driven through the ``custom`` adapter (via
:func:`ovid2.formatters.engine.execute_spec`) so the ``set_context`` hook
fires exactly as it does in the pipeline, with a stub input address
serving each PCORnet source identifier from an inline DataFrame (matched
on the exact identifier so overlapping names like ``OBS_CLIN`` /
``OBS_GEN`` never collide).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ovid2.formatters.adapters import AdapterContext
from ovid2.formatters.engine import execute_spec
from ovid2.formatters.spec import find_and_load

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


class _StubAddress:
    """Serve each PCORnet source identifier from an inline DataFrame.

    Matches on the exact source identifier (``glob`` then ``table_name``)
    so overlapping names never collide.
    """

    def __init__(self, tables: dict[str, DataFrame]) -> None:
        self._tables = tables

    def read(
        self,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        glob: str,
        table_name: str | None = None,
    ) -> DataFrame:
        for key in (glob, table_name):
            if key in self._tables:
                return self._tables[key]
        raise FileNotFoundError(glob)


def _run(table: str, spark: SparkSession, tables: dict[str, DataFrame]) -> DataFrame:
    from ovid2.core.engines.pyspark import PySparkEngine

    engine = PySparkEngine()
    engine._session = spark  # type: ignore[attr-defined]
    spec = find_and_load("pcornet_to_omop", table)
    ctx = AdapterContext(
        spark=spark,
        input_address=_StubAddress(tables),
        pipeline="pcornet_to_omop",
        batch_id="20260101",
        engine=engine,
    )
    return execute_spec(spec, ctx)


def _register_concept_views(spark: SparkSession) -> None:
    """Register concept / concept_relationship views across three domains.

    Codes:

    * ``4548-4`` (LOINC, Measurement) -> source 300, maps to 301
    * ``99213`` (CPT4, Procedure)    -> source 400, maps to 401
    * ``229819007`` (SNOMED, Observation) -> source 500, maps to 501
    * ``310965`` (RxNorm, Drug)      -> source 600, maps to 601
    """
    concept_schema = (
        "concept_id string, concept_code string, concept_name string, "
        "domain_id string, vocabulary_id string, concept_class_id string, "
        "standard_concept string, valid_start_date string, invalid_reason string"
    )
    concept_rows = [
        ("300", "4548-4", "HbA1c", "Measurement", "LOINC", "Lab", None, "2000-01-01", None),
        ("301", "4548-4-STD", "HbA1c STD", "Measurement", "LOINC", "Lab", "S", "2000-01-01", None),
        ("400", "99213", "Office visit", "Procedure", "CPT4", "CPT4", None, "2000-01-01", None),
        (
            "401",
            "99213-STD",
            "Office visit STD",
            "Procedure",
            "SNOMED",
            "Proc",
            "S",
            "2000-01-01",
            None,
        ),
        (
            "500",
            "229819007",
            "Tobacco",
            "Observation",
            "SNOMED",
            "Clinical",
            None,
            "2000-01-01",
            None,
        ),
        (
            "501",
            "229819007-STD",
            "Tobacco STD",
            "Observation",
            "SNOMED",
            "Clinical",
            "S",
            "2000-01-01",
            None,
        ),
        ("600", "310965", "Ibuprofen", "Drug", "RxNorm", "Ingredient", None, "2000-01-01", None),
        (
            "601",
            "310965-STD",
            "Ibuprofen STD",
            "Drug",
            "RxNorm",
            "Ingredient",
            "S",
            "2000-01-01",
            None,
        ),
    ]
    spark.createDataFrame(concept_rows, concept_schema).createOrReplaceTempView("concept")
    rel_schema = (
        "concept_id_1 string, concept_id_2 string, relationship_id string, "
        "valid_start_date string, valid_end_date string, invalid_reason string"
    )
    rel_rows = [
        ("300", "301", "Maps to", "2010-01-01", "2099-12-31", None),
        ("400", "401", "Maps to", "2010-01-01", "2099-12-31", None),
        ("500", "501", "Maps to", "2010-01-01", "2099-12-31", None),
        ("600", "601", "Maps to", "2010-01-01", "2099-12-31", None),
    ]
    spark.createDataFrame(rel_rows, rel_schema).createOrReplaceTempView("concept_relationship")


# --- source builders ---------------------------------------------------------


def _lab_result_cm(spark: SparkSession) -> DataFrame:
    cols = [
        "LAB_RESULT_CM_ID",
        "PATID",
        "ENCOUNTERID",
        "LAB_LOINC",
        "LAB_RESULT_SOURCE",
        "RESULT_DATE",
        "RESULT_TIME",
        "RESULT_NUM",
        "RESULT_QUAL",
        "RESULT_UNIT",
        "RESULT_MODIFIER",
        "NORM_RANGE_LOW",
        "NORM_RANGE_HIGH",
        "LAB_PX",
        "LAB_ORDER_DATE",
        "SPECIMEN_SOURCE",
        "SPECIMEN_DATE",
        "SOURCE",
    ]
    rows = [
        # HbA1c 4548-4 -> Measurement
        (
            "L1",
            "P1",
            "E1",
            "4548-4",
            "OD",
            "2021-04-01",
            "10:00:00",
            "6.5",
            "HIGH",
            "%",
            "EQ",
            "4.0",
            "6.0",
            "",
            "2021-03-30",
            "BLD",
            "2021-04-01",
            "SITE",
        ),
        # unmappable LOINC -> dropped from measurement
        (
            "L2",
            "P1",
            "E1",
            "0000-0",
            "OD",
            "2021-04-02",
            "11:00:00",
            "1.0",
            "NORMAL",
            "%",
            "EQ",
            "0",
            "2",
            "",
            "2021-04-01",
            "SER",
            "2021-04-02",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


def _procedures(spark: SparkSession) -> DataFrame:
    cols = [
        "PROCEDURESID",
        "PATID",
        "ENCOUNTERID",
        "PROVIDERID",
        "PX",
        "PX_TYPE",
        "PX_SOURCE",
        "PPX",
        "PX_DATE",
        "SOURCE",
    ]
    rows = [
        ("PX1", "P1", "E1", "PR1", "99213", "CH", "OD", "P", "2021-05-01", "SITE"),
    ]
    return spark.createDataFrame(rows, cols)


def _obs_clin(spark: SparkSession) -> DataFrame:
    cols = [
        "OBSCLINID",
        "PATID",
        "ENCOUNTERID",
        "OBSCLIN_PROVIDERID",
        "OBSCLIN_CODE",
        "OBSCLIN_TYPE",
        "OBSCLIN_SOURCE",
        "OBSCLIN_START_DATE",
        "OBSCLIN_START_TIME",
        "OBSCLIN_STOP_DATE",
        "OBSCLIN_STOP_TIME",
        "OBSCLIN_RESULT_NUM",
        "OBSCLIN_RESULT_QUAL",
        "OBSCLIN_RESULT_UNIT",
        "OBSCLIN_RESULT_MODIFIER",
        "OBSCLIN_RESULT_TEXT",
        "SOURCE",
    ]
    rows = [
        # tobacco SNOMED -> Observation domain
        (
            "OC1",
            "P1",
            "E1",
            "PR1",
            "229819007",
            "SM",
            "CL",
            "2021-06-01",
            "08:00:00",
            "2021-06-01",
            "08:30:00",
            "",
            "POSITIVE",
            "",
            "EQ",
            "smoker",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


def _obs_gen(spark: SparkSession) -> DataFrame:
    cols = [
        "OBSGENID",
        "PATID",
        "ENCOUNTERID",
        "OBSGEN_PROVIDERID",
        "OBSGEN_CODE",
        "OBSGEN_TYPE",
        "OBSGEN_SOURCE",
        "OBSGEN_START_DATE",
        "OBSGEN_START_TIME",
        "OBSGEN_STOP_DATE",
        "OBSGEN_STOP_TIME",
        "OBSGEN_RESULT_NUM",
        "OBSGEN_RESULT_QUAL",
        "OBSGEN_RESULT_UNIT",
        "OBSGEN_RESULT_MODIFIER",
        "OBSGEN_RESULT_TEXT",
        "SOURCE",
    ]
    rows = [
        (
            "OG1",
            "P2",
            "E2",
            "PR2",
            "4548-4",
            "LC",
            "CL",
            "2021-07-01",
            "09:00:00",
            "2021-07-01",
            "09:30:00",
            "7.1",
            "HIGH",
            "%",
            "EQ",
            "",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


def _vital(spark: SparkSession) -> DataFrame:
    cols = [
        "VITALID",
        "PATID",
        "ENCOUNTERID",
        "MEASURE_DATE",
        "MEASURE_TIME",
        "VITAL_SOURCE",
        "WT",
        "HT",
        "ORIGINAL_BMI",
        "DIASTOLIC",
        "SYSTOLIC",
        "BP_POSITION",
        "SMOKING",
        "TOBACCO",
        "TOBACCO_TYPE",
        "SOURCE",
    ]
    rows = [
        # tobacco 229819007 -> Observation; WT/HT etc use LOINC codes not in
        # the concept view so they drop out of every domain (that's fine —
        # this test only asserts the tobacco observation survives).
        (
            "V1",
            "P1",
            "E1",
            "2021-08-01",
            "07:00:00",
            "HC",
            "180",
            "70",
            "25.8",
            "80",
            "120",
            "01",
            "",
            "01",
            "",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


# --- measurement -------------------------------------------------------------


@pytest.mark.usefixtures("spark")
class TestMeasurementFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("measurement", spark, {"LAB_RESULT_CM": _lab_result_cm(spark)})
        assert set(out.columns) == {
            "measurement_id",
            "person_id",
            "measurement_concept_id",
            "measurement_date",
            "measurement_datetime",
            "measurement_time",
            "measurement_type_concept_id",
            "operator_concept_id",
            "value_as_number",
            "value_as_concept_id",
            "unit_concept_id",
            "range_low",
            "range_high",
            "provider_id",
            "visit_occurrence_id",
            "visit_detail_id",
            "measurement_source_value",
            "measurement_source_concept_id",
            "unit_source_value",
            "value_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_domain_filter_and_resolution(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("measurement", spark, {"LAB_RESULT_CM": _lab_result_cm(spark)})
        by_id = {r["measurement_id"]: r for r in out.collect()}
        # Only the mappable LOINC (L1) survives; L2 (0000-0) is filtered out.
        assert set(by_id) == {"L1"}
        assert by_id["L1"]["measurement_source_concept_id"] == "300"
        assert by_id["L1"]["measurement_concept_id"] == "301"
        assert by_id["L1"]["person_id"] == "P1"
        assert by_id["L1"]["visit_occurrence_id"] == "E1"
        assert by_id["L1"]["measurement_source_value"] == "4548-4"

    def test_obsgen_leg_reaches_measurement(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "measurement",
            spark,
            {"LAB_RESULT_CM": _lab_result_cm(spark), "OBS_GEN": _obs_gen(spark)},
        )
        by_id = {r["measurement_id"]: r for r in out.collect()}
        # OBS_GEN OG1 carries LOINC 4548-4 -> Measurement domain.
        assert "OG1" in by_id
        assert by_id["OG1"]["measurement_concept_id"] == "301"
        assert by_id["OG1"]["mapped_from"] == "OBS_GEN"


# --- procedure_occurrence ----------------------------------------------------


@pytest.mark.usefixtures("spark")
class TestProcedureOccurrenceFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "procedure_occurrence",
            spark,
            {"LAB_RESULT_CM": _lab_result_cm(spark), "PROCEDURES": _procedures(spark)},
        )
        assert set(out.columns) == {
            "procedure_occurrence_id",
            "person_id",
            "procedure_concept_id",
            "procedure_date",
            "procedure_datetime",
            "procedure_type_concept_id",
            "modifier_concept_id",
            "quantity",
            "provider_id",
            "visit_occurrence_id",
            "visit_detail_id",
            "procedure_source_value",
            "procedure_source_concept_id",
            "modifier_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_procedure_resolution_and_provider(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "procedure_occurrence",
            spark,
            {"LAB_RESULT_CM": _lab_result_cm(spark), "PROCEDURES": _procedures(spark)},
        )
        by_id = {r["procedure_occurrence_id"]: r for r in out.collect()}
        assert set(by_id) == {"PX1"}
        assert by_id["PX1"]["procedure_source_concept_id"] == "400"
        assert by_id["PX1"]["procedure_concept_id"] == "401"
        assert by_id["PX1"]["provider_id"] == "PR1"
        # PX_SOURCE OD -> type concept 32817
        assert by_id["PX1"]["procedure_type_concept_id"] == "32817"


# --- observation -------------------------------------------------------------


@pytest.mark.usefixtures("spark")
class TestObservationFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "observation",
            spark,
            {"LAB_RESULT_CM": _lab_result_cm(spark), "OBS_CLIN": _obs_clin(spark)},
        )
        assert set(out.columns) == {
            "observation_id",
            "person_id",
            "observation_concept_id",
            "observation_date",
            "observation_datetime",
            "observation_type_concept_id",
            "value_as_number",
            "value_as_string",
            "value_as_concept_id",
            "qualifier_concept_id",
            "unit_concept_id",
            "provider_id",
            "visit_occurrence_id",
            "visit_detail_id",
            "observation_source_value",
            "observation_source_concept_id",
            "unit_source_value",
            "qualifier_source_value",
            "value_source_concept_id",
            "value_source_value",
            "questionnaire_response_id",
            "updated",
            "source",
            "mapped_from",
        }

    def test_obs_clin_and_vital_reach_observation(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "observation",
            spark,
            {
                "LAB_RESULT_CM": _lab_result_cm(spark),
                "OBS_CLIN": _obs_clin(spark),
                "VITAL": _vital(spark),
            },
        )
        by_id = {r["observation_id"]: r for r in out.collect()}
        # OBS_CLIN tobacco 229819007 -> Observation domain.
        assert "OC1" in by_id
        assert by_id["OC1"]["observation_source_concept_id"] == "500"
        assert by_id["OC1"]["observation_concept_id"] == "501"
        assert by_id["OC1"]["value_as_string"] == "smoker"
        # VITAL tobacco branch (229819007) also reaches Observation.
        vital_ids = [k for k in by_id if k.startswith("V1_")]
        assert vital_ids, "expected a VITAL tobacco observation row"


# --- drug_exposure -----------------------------------------------------------


def _med_admin(spark: SparkSession) -> DataFrame:
    cols = [
        "MEDADMINID",
        "PATID",
        "ENCOUNTERID",
        "MEDADMIN_PROVIDERID",
        "MEDADMIN_CODE",
        "MEDADMIN_TYPE",
        "MEDADMIN_START_DATE",
        "MEDADMIN_START_TIME",
        "MEDADMIN_STOP_DATE",
        "MEDADMIN_STOP_TIME",
        "MEDADMIN_DOSE_ADMIN",
        "MEDADMIN_DOSE_ADMIN_UNIT",
        "MEDADMIN_ROUTE",
        "SOURCE",
    ]
    rows = [
        (
            "MA1",
            "P1",
            "E1",
            "PR1",
            "310965",
            "RX",
            "2021-09-01",
            "08:00:00",
            "2021-09-02",
            "08:00:00",
            "5",
            "mg",
            "ORAL",
            "SITE",
        ),
        # unmappable code -> dropped
        (
            "MA2",
            "P1",
            "E1",
            "PR1",
            "999999",
            "RX",
            "2021-09-01",
            "08:00:00",
            "2021-09-02",
            "08:00:00",
            "5",
            "mg",
            "ORAL",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


def _prescribing(spark: SparkSession) -> DataFrame:
    cols = [
        "PRESCRIBINGID",
        "PATID",
        "ENCOUNTERID",
        "RX_PROVIDERID",
        "RXNORM_CUI",
        "RX_START_DATE",
        "RX_END_DATE",
        "RX_REFILLS",
        "RX_QUANTITY",
        "RX_DAYS_SUPPLY",
        "RX_FREQUENCY",
        "RAW_RX_MED_NAME",
        "RX_ROUTE",
        "RX_DOSE_ORDERED_UNIT",
        "SOURCE",
    ]
    rows = [
        (
            "RX1",
            "P2",
            "E2",
            "PR2",
            "310965",
            "2021-10-01",
            "2021-10-10",
            "2",
            "30",
            "10",
            "02",
            "Ibuprofen",
            "ORAL",
            "mg",
            "SITE",
        ),
    ]
    return spark.createDataFrame(rows, cols)


@pytest.mark.usefixtures("spark")
class TestDrugExposureFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("drug_exposure", spark, {"MED_ADMIN": _med_admin(spark)})
        assert set(out.columns) == {
            "drug_exposure_id",
            "person_id",
            "drug_concept_id",
            "drug_exposure_start_date",
            "drug_exposure_start_datetime",
            "drug_exposure_end_date",
            "drug_exposure_end_datetime",
            "verbatim_end_date",
            "drug_type_concept_id",
            "stop_reason",
            "refills",
            "quantity",
            "days_supply",
            "sig",
            "route_concept_id",
            "lot_number",
            "provider_id",
            "visit_occurrence_id",
            "visit_detail_id",
            "drug_source_value",
            "drug_source_concept_id",
            "route_source_value",
            "dose_unit_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_med_admin_resolution_and_type(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("drug_exposure", spark, {"MED_ADMIN": _med_admin(spark)})
        by_id = {r["drug_exposure_id"]: r for r in out.collect()}
        # Only the mappable RxNorm (MA1) survives.
        assert set(by_id) == {"MA1"}
        assert by_id["MA1"]["drug_source_concept_id"] == "600"
        assert by_id["MA1"]["drug_concept_id"] == "601"
        assert by_id["MA1"]["drug_type_concept_id"] == "32830"
        # ORAL -> route concept 4132161
        assert by_id["MA1"]["route_concept_id"] == "4132161"
        assert by_id["MA1"]["provider_id"] == "PR1"

    def test_prescribing_leg_and_sig(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "drug_exposure",
            spark,
            {"MED_ADMIN": _med_admin(spark), "PRESCRIBING": _prescribing(spark)},
        )
        by_id = {r["drug_exposure_id"]: r for r in out.collect()}
        assert "RX1" in by_id
        assert by_id["RX1"]["drug_type_concept_id"] == "32833"
        assert by_id["RX1"]["mapped_from"] == "PRESCRIBING"
        # RX_FREQUENCY 02 -> BID sig text on RAW_RX_MED_NAME
        assert "Two times a day (BID)" in by_id["RX1"]["sig"]
        assert by_id["RX1"]["refills"] == "2"


# --- specimen ----------------------------------------------------------------


@pytest.mark.usefixtures("spark")
class TestSpecimenFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("specimen", spark, {"LAB_RESULT_CM": _lab_result_cm(spark)})
        assert set(out.columns) == {
            "specimen_id",
            "person_id",
            "specimen_concept_id",
            "specimen_type_concept_id",
            "specimen_date",
            "specimen_datetime",
            "quantity",
            "unit_concept_id",
            "anatomic_site_concept_id",
            "disease_status_concept_id",
            "specimen_source_id",
            "specimen_source_value",
            "unit_source_value",
            "anatomic_site_source_value",
            "disease_status_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_specimen_concept_and_id(self, spark: SparkSession) -> None:
        out = _run("specimen", spark, {"LAB_RESULT_CM": _lab_result_cm(spark)})
        by_id = {r["specimen_id"]: r for r in out.collect()}
        assert set(by_id) == {"L1", "L2"}
        # BLD -> 4001225; SER -> 4001181
        assert by_id["L1"]["specimen_concept_id"] == "4001225"
        assert by_id["L2"]["specimen_concept_id"] == "4001181"
        assert by_id["L1"]["specimen_source_id"] == "L1"
        # legacy lit(0) placeholders
        assert by_id["L1"]["specimen_type_concept_id"] == "0"
        assert by_id["L1"]["unit_concept_id"] == "0"


# --- header-only tables ------------------------------------------------------


@pytest.mark.usefixtures("spark")
class TestHeaderOnlyFormatters:
    def _demographic(self, spark: SparkSession) -> DataFrame:
        return spark.createDataFrame(
            [("P1", "1980-01-01", "F", "05", "N", "SITE")],
            ["PATID", "BIRTH_DATE", "SEX", "RACE", "HISPANIC", "SOURCE"],
        )

    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            (
                "device_exposure",
                {
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
                },
            ),
            (
                "note",
                {
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
                },
            ),
            (
                "fact_relationship",
                {
                    "domain_concept_id_1",
                    "fact_id_1",
                    "domain_concept_id_2",
                    "fact_id_2",
                    "relationship_concept_id",
                    "updated",
                    "source",
                    "mapped_from",
                },
            ),
        ],
    )
    def test_header_only_columns_and_empty(
        self, spark: SparkSession, table: str, expected: set[str]
    ) -> None:
        out = _run(table, spark, {"DEMOGRAPHIC": self._demographic(spark)})
        assert set(out.columns) == expected
        assert out.count() == 0

    def test_visit_detail_columns(self, spark: SparkSession) -> None:
        out = _run("visit_detail", spark, {"DEMOGRAPHIC": self._demographic(spark)})
        assert "visit_detail_id" in out.columns
        assert "visit_detail_parent_id" in out.columns
        assert out.count() == 0
