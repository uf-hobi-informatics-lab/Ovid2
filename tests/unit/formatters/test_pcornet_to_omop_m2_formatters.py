"""Execution unit tests for the M2 PCORnet -> OMOP converter formatters.

Covers the reference / patient-timeline tier added in M2:
``death``, ``observation_period``, ``cdm_source``, ``provider``,
``care_site`` and ``location``. Each formatter is driven through the
``custom`` adapter (via :func:`ovid2.formatters.engine.execute_spec`) so
the ``set_context`` hook fires exactly as it does in the pipeline, with a
stub input address serving each PCORnet source identifier from an inline
DataFrame.

These assert the M2 conversion logic: PCORnet primary keys reused as OMOP
ids where a natural key exists (``person_id = PATID``), deterministic
``hash_key_id`` surrogate ids where none does (observation_period /
provider / care_site / location), static concept-id dictionaries applied,
concept resolution for death causes, facility de-duplication, and the OMOP
output column shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ovid2_pipelines.pcornet_to_omop.formatters_custom._helpers import hash_key_id

from ovid2.formatters.adapters import AdapterContext
from ovid2.formatters.engine import execute_spec
from ovid2.formatters.spec import find_and_load

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


class _StubAddress:
    """Serve each PCORnet source identifier from an inline DataFrame.

    Matches on the exact source identifier (``table_name`` or ``glob``) so
    overlapping names like ``DEATH`` / ``DEATH_CAUSE`` never collide.
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
        # The custom adapter's primary read passes table_name=<formatter
        # name> (e.g. "death") + glob=<input_file_name> (e.g. "DEATH");
        # sibling reads pass table_name=glob=<identifier> (e.g.
        # "DEATH_CAUSE"). Prefer an exact glob match, then table_name, so
        # overlapping names like DEATH / DEATH_CAUSE never collide.
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


def _expected_hash(spark: SparkSession, *values: str) -> str:
    """Compute the same hash_key_id a formatter would, for assertions."""
    from pyspark.sql.functions import lit

    from ovid2.core.engines.pyspark import PySparkEngine

    fns = PySparkEngine().get_functions()
    cols = [lit(v).alias(f"c{i}") for i, v in enumerate(values)]
    row = (
        spark.range(1)
        .select(*cols)
        .select(hash_key_id(fns, *[f"c{i}" for i in range(len(values))]).alias("h"))
        .collect()[0]
    )
    return row["h"]


# --- observation_period -----------------------------------------------------


def _enrollment(spark: SparkSession) -> DataFrame:
    rows = [
        # PATID, ENR_START_DATE, ENR_END_DATE, ENR_BASIS, SOURCE
        ("P1", "2018-01-01", "2020-12-31", "I", "SITE"),
        ("P2", "2019-06-01", "", "A", "SITE"),
    ]
    return spark.createDataFrame(
        rows, ["PATID", "ENR_START_DATE", "ENR_END_DATE", "ENR_BASIS", "SOURCE"]
    )


@pytest.mark.usefixtures("spark")
class TestObservationPeriodFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("observation_period", spark, {"ENROLLMENT": _enrollment(spark)})
        assert set(out.columns) == {
            "observation_period_id",
            "person_id",
            "observation_period_start_date",
            "observation_period_end_date",
            "period_type_concept_id",
            "updated",
            "source",
            "mapped_from",
        }

    def test_id_hash_and_person_id(self, spark: SparkSession) -> None:
        out = _run("observation_period", spark, {"ENROLLMENT": _enrollment(spark)})
        by_person = {r["person_id"]: r for r in out.collect()}
        assert set(by_person) == {"P1", "P2"}
        expected = _expected_hash(spark, "P1", "2018-01-01", "I")
        assert by_person["P1"]["observation_period_id"] == expected
        # deterministic, non-negative
        assert int(by_person["P1"]["observation_period_id"]) >= 0

    def test_period_type_and_sentinel(self, spark: SparkSession) -> None:
        out = _run("observation_period", spark, {"ENROLLMENT": _enrollment(spark)})
        by_person = {r["person_id"]: r for r in out.collect()}
        # ENR_BASIS I -> 32813, A -> 32880
        assert by_person["P1"]["period_type_concept_id"] == "32813"
        assert by_person["P2"]["period_type_concept_id"] == "32880"
        # P2 has no end date -> 1900-01-01 sentinel
        assert str(by_person["P2"]["observation_period_end_date"]) == "1900-01-01"


# --- provider ----------------------------------------------------------------


def _provider(spark: SparkSession) -> DataFrame:
    rows = [
        # PROVIDERID, PROVIDER_NPI, PROVIDER_SEX, PROVIDER_SPECIALTY_PRIMARY, SOURCE
        ("PR1", "1234567890", "F", "207R00000X", "SITE"),
        ("PR2", "9876543210", "M", "", "SITE"),
    ]
    return spark.createDataFrame(
        rows,
        [
            "PROVIDERID",
            "PROVIDER_NPI",
            "PROVIDER_SEX",
            "PROVIDER_SPECIALTY_PRIMARY",
            "SOURCE",
        ],
    )


@pytest.mark.usefixtures("spark")
class TestProviderFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("provider", spark, {"PROVIDER": _provider(spark)})
        assert set(out.columns) == {
            "provider_id",
            "provider_name",
            "npi",
            "dea",
            "care_site_id",
            "year_of_birth",
            "specialty_concept_id",
            "gender_concept_id",
            "provider_source_value",
            "specialty_source_value",
            "specialty_source_concept_id",
            "gender_source_value",
            "gender_source_concept_id",
            "updated",
            "source",
            "mapped_from",
        }

    def test_id_hash_and_gender(self, spark: SparkSession) -> None:
        out = _run("provider", spark, {"PROVIDER": _provider(spark)})
        by_src = {r["provider_source_value"]: r for r in out.collect()}
        assert set(by_src) == {"PR1", "PR2"}
        assert by_src["PR1"]["provider_id"] == _expected_hash(spark, "PR1")
        assert by_src["PR1"]["npi"] == "1234567890"
        # gender F -> 8532, M -> 8507
        assert by_src["PR1"]["gender_concept_id"] == "8532"
        assert by_src["PR2"]["gender_concept_id"] == "8507"
        # specialty concept left at 0; raw code preserved
        assert by_src["PR1"]["specialty_concept_id"] == "0"
        assert by_src["PR1"]["specialty_source_value"] == "207R00000X"


# --- care_site / location (derived from ENCOUNTER) --------------------------


def _encounter(spark: SparkSession) -> DataFrame:
    rows = [
        # ENCOUNTERID, FACILITYID, FACILITY_TYPE, FACILITY_LOCATION, SOURCE
        ("E1", "F1", "HOSPITAL_COMMUNITY", "32601", "SITE"),
        ("E2", "F1", "HOSPITAL_COMMUNITY", "32601", "SITE"),  # dup facility
        ("E3", "F2", "SKILLED_NURSING_FACILITY", "32608", "SITE"),
    ]
    return spark.createDataFrame(
        rows,
        ["ENCOUNTERID", "FACILITYID", "FACILITY_TYPE", "FACILITY_LOCATION", "SOURCE"],
    )


@pytest.mark.usefixtures("spark")
class TestCareSiteFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("care_site", spark, {"ENCOUNTER": _encounter(spark)})
        assert set(out.columns) == {
            "care_site_id",
            "care_site_name",
            "place_of_service_concept_id",
            "location_id",
            "care_site_source_value",
            "place_of_service_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_dedup_ids_and_pos(self, spark: SparkSession) -> None:
        out = _run("care_site", spark, {"ENCOUNTER": _encounter(spark)})
        rows = out.collect()
        by_name = {r["care_site_source_value"]: r for r in rows}
        # F1 appears twice on ENCOUNTER -> one care_site row
        assert len(rows) == 2
        assert set(by_name) == {"F1", "F2"}
        assert by_name["F1"]["care_site_id"] == _expected_hash(spark, "F1")
        # location_id matches the location formatter's key on FACILITY_LOCATION
        assert by_name["F1"]["location_id"] == _expected_hash(spark, "32601")
        # HOSPITAL_COMMUNITY -> 4021523; SKILLED_NURSING_FACILITY -> 4164912
        assert by_name["F1"]["place_of_service_concept_id"] == "4021523"
        assert by_name["F2"]["place_of_service_concept_id"] == "4164912"


@pytest.mark.usefixtures("spark")
class TestLocationFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("location", spark, {"ENCOUNTER": _encounter(spark)})
        assert set(out.columns) == {
            "location_id",
            "address_1",
            "address_2",
            "city",
            "state",
            "zip",
            "county",
            "location_source_value",
            "updated",
            "source",
            "mapped_from",
        }

    def test_dedup_and_zip(self, spark: SparkSession) -> None:
        out = _run("location", spark, {"ENCOUNTER": _encounter(spark)})
        rows = out.collect()
        by_zip = {r["zip"]: r for r in rows}
        # two distinct FACILITY_LOCATION values
        assert set(by_zip) == {"32601", "32608"}
        assert by_zip["32601"]["location_id"] == _expected_hash(spark, "32601")
        assert by_zip["32601"]["location_source_value"] == "32601"

    def test_location_id_matches_care_site(self, spark: SparkSession) -> None:
        loc = _run("location", spark, {"ENCOUNTER": _encounter(spark)})
        cs = _run("care_site", spark, {"ENCOUNTER": _encounter(spark)})
        loc_ids = {r["zip"]: r["location_id"] for r in loc.collect()}
        cs_loc_ids = {r["care_site_source_value"]: r["location_id"] for r in cs.collect()}
        # care_site F1 (location 32601) points at the same location_id row
        assert cs_loc_ids["F1"] == loc_ids["32601"]


# --- cdm_source --------------------------------------------------------------


def _settings_holder() -> str:
    """Return the deployment's configured ``CDM_HOLDER`` (may be empty)."""
    from ovid2_pipelines.pcornet_to_omop import pipeline_settings

    return str(getattr(pipeline_settings, "CDM_HOLDER", "") or "")


def _harvest(spark: SparkSession) -> DataFrame:
    rows = [
        # DATAMART_NAME, DATAMARTID, REFRESH_ENCOUNTER_DATE
        ("Example Network", "EXN01", "2026-01-15"),
    ]
    return spark.createDataFrame(rows, ["DATAMART_NAME", "DATAMARTID", "REFRESH_ENCOUNTER_DATE"])


@pytest.mark.usefixtures("spark")
class TestCdmSourceFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        out = _run("cdm_source", spark, {"HARVEST": _harvest(spark)})
        assert set(out.columns) == {
            "cdm_source_name",
            "cdm_source_abbreviation",
            "cdm_holder",
            "source_documentation_reference",
            "cdm_etl_reference",
            "source_release_date",
            "cdm_release_date",
            "cdm_version",
            "vocabulary_version",
        }

    def test_metadata_row(self, spark: SparkSession) -> None:
        out = _run("cdm_source", spark, {"HARVEST": _harvest(spark)})
        rows = out.collect()
        assert len(rows) == 1
        r = rows[0]
        assert r["cdm_source_name"] == "Example Network"
        assert r["cdm_source_abbreviation"] == "EXN01"
        assert r["cdm_holder"] == _settings_holder()
        assert r["cdm_version"] == "5.3"
        assert str(r["source_release_date"]) == "2026-01-15"


# --- death -------------------------------------------------------------------


def _register_concept_views(spark: SparkSession) -> None:
    concept_schema = (
        "concept_id string, concept_code string, concept_name string, "
        "domain_id string, vocabulary_id string, concept_class_id string, "
        "standard_concept string, valid_start_date string, invalid_reason string"
    )
    concept_rows = [
        ("100", "I25.9", "IHD", "Condition", "ICD-10-CM", "4-char", None, "2000-01-01", None),
        (
            "200",
            "414545008",
            "IHD SNOMED",
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
    rel_rows = [("100", "200", "Maps to", "2010-01-01", "2099-12-31", None)]
    spark.createDataFrame(rel_rows, rel_schema).createOrReplaceTempView("concept_relationship")


def _death(spark: SparkSession) -> DataFrame:
    rows = [
        # PATID, DEATH_DATE, DEATH_SOURCE, SOURCE
        ("P1", "2022-02-02", "L", "SITE"),
        ("P2", "", "N", "SITE"),
    ]
    return spark.createDataFrame(rows, ["PATID", "DEATH_DATE", "DEATH_SOURCE", "SOURCE"])


def _death_cause(spark: SparkSession) -> DataFrame:
    rows = [
        # PATID, DEATH_CAUSE, DEATH_CAUSE_SOURCE
        ("P1", "I25.9", "L"),
    ]
    return spark.createDataFrame(rows, ["PATID", "DEATH_CAUSE", "DEATH_CAUSE_SOURCE"])


@pytest.mark.usefixtures("spark")
class TestDeathFormatter:
    def test_output_columns(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "death",
            spark,
            {"DEATH_CAUSE": _death_cause(spark), "DEATH": _death(spark)},
        )
        assert set(out.columns) == {
            "person_id",
            "death_date",
            "death_datetime",
            "death_type_concept_id",
            "cause_concept_id",
            "cause_source_value",
            "cause_source_concept_id",
            "updated",
            "source",
            "mapped_from",
        }

    def test_person_id_and_type_and_cause(self, spark: SparkSession) -> None:
        _register_concept_views(spark)
        out = _run(
            "death",
            spark,
            {"DEATH_CAUSE": _death_cause(spark), "DEATH": _death(spark)},
        )
        by_person = {r["person_id"]: r for r in out.collect()}
        assert set(by_person) == {"P1", "P2"}
        # DEATH_SOURCE L -> 32817, N -> 32815
        assert by_person["P1"]["death_type_concept_id"] == "32817"
        assert by_person["P2"]["death_type_concept_id"] == "32815"
        # P1 cause I25.9 resolves: source concept 100, 'Maps to' -> 200
        assert by_person["P1"]["cause_source_concept_id"] == "100"
        assert by_person["P1"]["cause_concept_id"] == "200"
        assert by_person["P1"]["cause_source_value"] == "I25.9"
        # P2 has no death cause -> null cause, but still a death row
        assert by_person["P2"]["cause_concept_id"] is None
        # P2 has no death date -> sentinel
        assert str(by_person["P2"]["death_date"]) == "1900-01-01"

    def test_death_without_cause_sibling(self, spark: SparkSession) -> None:
        # DEATH_CAUSE absent entirely: every DEATH row still emitted, blank cause.
        out = _run("death", spark, {"DEATH": _death(spark)})
        by_person = {r["person_id"]: r for r in out.collect()}
        assert set(by_person) == {"P1", "P2"}
        assert by_person["P1"]["cause_source_value"] == ""
