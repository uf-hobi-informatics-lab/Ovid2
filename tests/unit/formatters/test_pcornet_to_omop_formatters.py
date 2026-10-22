"""Execution unit tests for the PCORnet -> OMOP converter formatters.

Each formatter is driven through the ``custom`` adapter (via
:func:`ovid2.formatters.engine.execute_spec`) so the ``set_context`` hook
fires exactly as it does in the pipeline. A stub input address serves the
PCORnet source identifier from an inline DataFrame.

These assert the conversion logic: PCORnet primary keys reused as OMOP
ids, static concept-id dictionaries applied, and the OMOP output column
shape produced.
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
    """Serve each PCORnet source identifier from an inline DataFrame."""

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
        for key in sorted(self._tables, key=len, reverse=True):
            if key in glob:
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


def _demographic(spark: SparkSession) -> DataFrame:
    rows = [
        # PATID, BIRTH_DATE, SEX, RACE, HISPANIC, SOURCE
        ("P1", "1980-05-15", "F", "05", "N", "SITE"),
        ("P2", "1975-01-01", "M", "03", "Y", "SITE"),
        ("P3", "2000-12-31", "UN", "07", "R", "SITE"),
        ("P4", "1990-06-06", "", "NI", "", "SITE"),
    ]
    return spark.createDataFrame(rows, ["PATID", "BIRTH_DATE", "SEX", "RACE", "HISPANIC", "SOURCE"])


@pytest.mark.usefixtures("spark")
class TestPersonFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("person", spark, {"DEMOGRAPHIC": _demographic(spark)})
        expected = {
            "person_id",
            "gender_concept_id",
            "year_of_birth",
            "month_of_birth",
            "day_of_birth",
            "birth_datetime",
            "race_concept_id",
            "ethnicity_concept_id",
            "location_id",
            "provider_id",
            "care_site_id",
            "person_source_value",
            "gender_source_value",
            "gender_source_concept_id",
            "race_source_value",
            "race_source_concept_id",
            "ethnicity_source_value",
            "ethnicity_source_concept_id",
            "updated",
            "source",
            "mapped_from",
        }
        assert set(out.columns) == expected

    def test_person_id_is_patid(self, spark: SparkSession) -> None:
        out = _run("person", spark, {"DEMOGRAPHIC": _demographic(spark)})
        pairs = {r["person_id"]: r["person_source_value"] for r in out.collect()}
        assert pairs == {"P1": "P1", "P2": "P2", "P3": "P3", "P4": "P4"}

    def test_concept_id_dictionaries(self, spark: SparkSession) -> None:
        out = _run("person", spark, {"DEMOGRAPHIC": _demographic(spark)})
        by_id = {r["person_id"]: r for r in out.collect()}
        # F -> 8532, M -> 8507, UN -> 4214687, '' -> 0
        assert by_id["P1"]["gender_concept_id"] == "8532"
        assert by_id["P2"]["gender_concept_id"] == "8507"
        assert by_id["P3"]["gender_concept_id"] == "4214687"
        assert by_id["P4"]["gender_concept_id"] == "0"
        # RACE 05 -> 8527, 03 -> 8516, 07 -> 2090000003, NI -> 0
        assert by_id["P1"]["race_concept_id"] == "8527"
        assert by_id["P2"]["race_concept_id"] == "8516"
        assert by_id["P3"]["race_concept_id"] == "2090000003"
        assert by_id["P4"]["race_concept_id"] == "0"
        # HISPANIC N -> 38003564, Y -> 38003563, R -> 2090000003, '' -> 0
        assert by_id["P1"]["ethnicity_concept_id"] == "38003564"
        assert by_id["P2"]["ethnicity_concept_id"] == "38003563"
        assert by_id["P3"]["ethnicity_concept_id"] == "2090000003"
        assert by_id["P4"]["ethnicity_concept_id"] == "0"

    def test_birth_date_split(self, spark: SparkSession) -> None:
        out = _run("person", spark, {"DEMOGRAPHIC": _demographic(spark)})
        by_id = {r["person_id"]: r for r in out.collect()}
        assert by_id["P1"]["year_of_birth"] == "1980"
        assert by_id["P1"]["month_of_birth"] == "05"
        assert by_id["P1"]["day_of_birth"] == "15"
        assert by_id["P1"]["mapped_from"] == "DEMOGRAPHIC"


def _encounter(spark: SparkSession) -> DataFrame:
    rows = [
        # ENCOUNTERID, PATID, ENC_TYPE, ADMIT_DATE, DISCHARGE_DATE,
        # PROVIDERID, FACILITYID, ADMITTING_SOURCE, DISCHARGE_STATUS, SOURCE
        ("E1", "P1", "IP", "2021-03-01", "2021-03-05", "PR1", "F1", "ED", "HO", "SITE"),
        ("E2", "P2", "AV", "2021-04-01", "", "PR2", "F2", "", "", "SITE"),
    ]
    return spark.createDataFrame(
        rows,
        [
            "ENCOUNTERID",
            "PATID",
            "ENC_TYPE",
            "ADMIT_DATE",
            "DISCHARGE_DATE",
            "PROVIDERID",
            "FACILITYID",
            "ADMITTING_SOURCE",
            "DISCHARGE_STATUS",
            "SOURCE",
        ],
    )


@pytest.mark.usefixtures("spark")
class TestVisitOccurrenceFormatter:
    def test_ids_and_concepts(self, spark: SparkSession) -> None:
        out = _run("visit_occurrence", spark, {"ENCOUNTER": _encounter(spark)})
        by_id = {r["visit_occurrence_id"]: r for r in out.collect()}
        # visit_occurrence_id = ENCOUNTERID, person_id = PATID
        assert set(by_id) == {"E1", "E2"}
        assert by_id["E1"]["person_id"] == "P1"
        # ENC_TYPE IP -> 9201, AV -> 38004207
        assert by_id["E1"]["visit_concept_id"] == "9201"
        assert by_id["E2"]["visit_concept_id"] == "38004207"
        # visit_type_concept_id is the constant EHR-encounter concept.
        assert by_id["E1"]["visit_type_concept_id"] == "32035"
        # provider_id / care_site_id reuse PCORnet ids verbatim.
        assert by_id["E1"]["provider_id"] == "PR1"
        assert by_id["E1"]["care_site_id"] == "F1"

    def test_date_sentinel(self, spark: SparkSession) -> None:
        out = _run("visit_occurrence", spark, {"ENCOUNTER": _encounter(spark)})
        by_id = {r["visit_occurrence_id"]: r for r in out.collect()}
        # E2 has no discharge date -> 1900-01-01 sentinel.
        assert str(by_id["E2"]["visit_end_date"]) == "1900-01-01"


def _register_concept_views(spark: SparkSession) -> None:
    """Register minimal ``concept`` + ``concept_relationship`` temp views."""
    concept_schema = (
        "concept_id string, concept_code string, concept_name string, "
        "domain_id string, vocabulary_id string, concept_class_id string, "
        "standard_concept string, valid_start_date string, invalid_reason string"
    )
    concept_rows = [
        ("100", "E11.9", "T2DM", "Condition", "ICD-10-CM", "4-char", None, "2000-01-01", None),
        (
            "200",
            "201826",
            "T2DM SNOMED",
            "Condition",
            "SNOMED",
            "Clinical",
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
        ("100", "200", "Maps to", "2010-01-01", "2099-12-31", None),
    ]
    spark.createDataFrame(rel_rows, rel_schema).createOrReplaceTempView("concept_relationship")


def _diagnosis(spark: SparkSession) -> DataFrame:
    rows = [
        # DIAGNOSISID, PATID, ENCOUNTERID, DX, DX_TYPE, DX_ORIGIN, DX_SOURCE,
        # ADMIT_DATE, DX_DATE, SOURCE
        ("D1", "P1", "E1", "E11.9", "10", "OD", "AD", "2021-03-01", "2021-03-01", "SITE"),
        # unmappable code -> dropped by the inner source-concept join
        ("D2", "P1", "E1", "ZZZ", "10", "OD", "AD", "2021-03-01", "2021-03-01", "SITE"),
    ]
    return spark.createDataFrame(
        rows,
        [
            "DIAGNOSISID",
            "PATID",
            "ENCOUNTERID",
            "DX",
            "DX_TYPE",
            "DX_ORIGIN",
            "DX_SOURCE",
            "ADMIT_DATE",
            "DX_DATE",
            "SOURCE",
        ],
    )


@pytest.mark.usefixtures("spark")
class TestConditionOccurrenceFormatter:
    def test_concept_resolution_and_filter(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("condition_occurrence", spark, {"DIAGNOSIS": _diagnosis(spark)})
        rows = out.collect()
        by_id = {r["condition_occurrence_id"]: r for r in rows}
        # Only the mappable ICD-10 code survives (inner source-concept join).
        assert set(by_id) == {"D1"}
        r = by_id["D1"]
        assert r["person_id"] == "P1"
        assert r["visit_occurrence_id"] == "E1"
        # source_concept_id = the ICD-10-CM concept (100); concept_id via
        # 'Maps to' -> the standard SNOMED concept (200).
        assert r["condition_source_concept_id"] == "100"
        assert r["condition_concept_id"] == "200"
        assert r["condition_source_value"] == "E11.9"
        assert r["mapped_from"] == "DIAGNOSIS"

    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run("condition_occurrence", spark, {"DIAGNOSIS": _diagnosis(spark)})
        expected = {
            "condition_occurrence_id",
            "person_id",
            "condition_concept_id",
            "condition_start_date",
            "condition_start_datetime",
            "condition_end_date",
            "condition_end_datetime",
            "condition_type_concept_id",
            "condition_status_concept_id",
            "stop_reason",
            "provider_id",
            "visit_occurrence_id",
            "visit_detail_id",
            "condition_source_value",
            "condition_source_concept_id",
            "condition_status_source_value",
            "updated",
            "source",
            "mapped_from",
        }
        assert set(out.columns) == expected
