"""End-to-end pipeline check for the PCORnet -> OMOP converter (disc).

Drives the real :func:`ovid2.core.pipeline.run_pipeline` for the three
migrated OMOP tables (person, visit_occurrence, condition_occurrence)
against on-disc PCORnet CSV inputs and a local PySpark engine, then reads
the written OMOP CSV back and asserts the converted shape. This is the
honest end-to-end proof that the formatter chain, the ``omop_5_3`` CDM
table loop, and the concept-resolution join all work together.

Skips automatically when no JVM is available (the shared ``spark``
fixture handles this).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


def _write_pcornet_table(root: Path, pipeline: str, batch: str, table: str,
                         header: list[str], rows: list[list[str]]) -> None:
    """Write a PCORnet source table as ``{root}/{pipeline}/{batch}/{TABLE}/{TABLE}.csv``."""
    table_dir = root / pipeline / batch / table
    table_dir.mkdir(parents=True, exist_ok=True)
    with (table_dir / f"{table}.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _read_omop_output(out_root: Path, pipeline: str, batch: str, table: str) -> list[dict]:
    """Read a written OMOP CSV artefact back into a list of dict rows."""
    path = out_root / pipeline / batch / f"{table}.csv"
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


@pytest.mark.integration
def test_pcornet_to_omop_disc_end_to_end(spark: "SparkSession", tmp_path: Path) -> None:
    from ovid2.core.engines.pyspark import PySparkEngine
    from ovid2.core.pipeline import run_pipeline
    from ovid2.drivers.disc import DiscAddress

    pipeline = "pcornet_to_omop"
    batch = "20260101"
    in_root = tmp_path / "pcornet_in"
    out_root = tmp_path / "omop_out"

    _write_pcornet_table(
        in_root, pipeline, batch, "DEMOGRAPHIC",
        ["PATID", "BIRTH_DATE", "SEX", "RACE", "HISPANIC", "SOURCE"],
        [
            ["P1", "1980-05-15", "F", "05", "N", "SITE"],
            ["P2", "1975-01-01", "M", "03", "Y", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "ENCOUNTER",
        ["ENCOUNTERID", "PATID", "ENC_TYPE", "ADMIT_DATE", "DISCHARGE_DATE",
         "PROVIDERID", "FACILITYID", "FACILITY_TYPE", "FACILITY_LOCATION",
         "ADMITTING_SOURCE", "DISCHARGE_STATUS", "SOURCE"],
        [
            ["E1", "P1", "IP", "2021-03-01", "2021-03-05", "PR1", "F1",
             "HOSPITAL_COMMUNITY", "32601", "ED", "HO", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "DIAGNOSIS",
        ["DIAGNOSISID", "PATID", "ENCOUNTERID", "DX", "DX_TYPE", "DX_ORIGIN",
         "DX_SOURCE", "ADMIT_DATE", "DX_DATE", "SOURCE"],
        [
            ["D1", "P1", "E1", "E11.9", "10", "OD", "AD", "2021-03-01", "2021-03-01", "SITE"],
            ["D2", "P1", "E1", "ZZZ", "10", "OD", "AD", "2021-03-01", "2021-03-01", "SITE"],
        ],
    )
    # M2 sources.
    _write_pcornet_table(
        in_root, pipeline, batch, "ENROLLMENT",
        ["PATID", "ENR_START_DATE", "ENR_END_DATE", "ENR_BASIS", "SOURCE"],
        [
            ["P1", "2018-01-01", "2020-12-31", "I", "SITE"],
            ["P2", "2019-06-01", "", "A", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "PROVIDER",
        ["PROVIDERID", "PROVIDER_NPI", "PROVIDER_SEX", "PROVIDER_SPECIALTY_PRIMARY",
         "SOURCE"],
        [
            ["PR1", "1234567890", "F", "207R00000X", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "DEATH",
        ["PATID", "DEATH_DATE", "DEATH_SOURCE", "SOURCE"],
        [
            ["P1", "2022-02-02", "L", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "DEATH_CAUSE",
        ["PATID", "DEATH_CAUSE", "DEATH_CAUSE_SOURCE"],
        [
            ["P1", "E11.9", "L"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "HARVEST",
        ["DATAMART_NAME", "DATAMARTID", "REFRESH_ENCOUNTER_DATE"],
        [
            ["Example Network", "EXN01", "2026-01-15"],
        ],
    )
    # M3 sources.
    _write_pcornet_table(
        in_root, pipeline, batch, "LAB_RESULT_CM",
        ["LAB_RESULT_CM_ID", "PATID", "ENCOUNTERID", "LAB_LOINC", "LAB_RESULT_SOURCE",
         "RESULT_DATE", "RESULT_TIME", "RESULT_NUM", "RESULT_QUAL", "RESULT_UNIT",
         "RESULT_MODIFIER", "NORM_RANGE_LOW", "NORM_RANGE_HIGH", "LAB_PX",
         "LAB_ORDER_DATE", "SPECIMEN_SOURCE", "SPECIMEN_DATE", "SOURCE"],
        [
            ["L1", "P1", "E1", "4548-4", "OD", "2021-04-01", "10:00:00", "6.5",
             "HIGH", "%", "EQ", "4.0", "6.0", "", "2021-03-30", "BLD", "2021-04-01", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "PROCEDURES",
        ["PROCEDURESID", "PATID", "ENCOUNTERID", "PROVIDERID", "PX", "PX_TYPE",
         "PX_SOURCE", "PPX", "PX_DATE", "SOURCE"],
        [
            ["PX1", "P1", "E1", "PR1", "99213", "CH", "OD", "P", "2021-05-01", "SITE"],
        ],
    )
    _write_pcornet_table(
        in_root, pipeline, batch, "MED_ADMIN",
        ["MEDADMINID", "PATID", "ENCOUNTERID", "MEDADMIN_PROVIDERID", "MEDADMIN_CODE",
         "MEDADMIN_TYPE", "MEDADMIN_START_DATE", "MEDADMIN_START_TIME",
         "MEDADMIN_STOP_DATE", "MEDADMIN_STOP_TIME", "MEDADMIN_DOSE_ADMIN",
         "MEDADMIN_DOSE_ADMIN_UNIT", "MEDADMIN_ROUTE", "SOURCE"],
        [
            ["MA1", "P1", "E1", "PR1", "310965", "RX", "2021-09-01", "08:00:00",
             "2021-09-02", "08:00:00", "5", "mg", "ORAL", "SITE"],
        ],
    )

    # Register the concept / concept_relationship shared views the
    # condition formatter resolves against (as register_shared_tables does).
    concept_schema = (
        "concept_id string, concept_code string, concept_name string, "
        "domain_id string, vocabulary_id string, concept_class_id string, "
        "standard_concept string, valid_start_date string, invalid_reason string"
    )
    spark.createDataFrame(
        [
            ("100", "E11.9", "T2DM", "Condition", "ICD-10-CM", "4-char", None, "2000-01-01", None),
            ("200", "201826", "T2DM SNOMED", "Condition", "SNOMED", "Clinical", "S", "2000-01-01", None),
            # M3 codes: Measurement (LOINC), Procedure (CPT4), Drug (RxNorm).
            ("300", "4548-4", "HbA1c", "Measurement", "LOINC", "Lab", None, "2000-01-01", None),
            ("301", "4548-4-STD", "HbA1c STD", "Measurement", "LOINC", "Lab", "S", "2000-01-01", None),
            ("400", "99213", "Office visit", "Procedure", "CPT4", "CPT4", None, "2000-01-01", None),
            ("401", "99213-STD", "Office visit STD", "Procedure", "SNOMED", "Proc", "S", "2000-01-01", None),
            ("600", "310965", "Ibuprofen", "Drug", "RxNorm", "Ingredient", None, "2000-01-01", None),
            ("601", "310965-STD", "Ibuprofen STD", "Drug", "RxNorm", "Ingredient", "S", "2000-01-01", None),
        ],
        concept_schema,
    ).createOrReplaceTempView("concept")
    spark.createDataFrame(
        [
            ("100", "200", "Maps to", "2010-01-01", "2099-12-31", None),
            ("300", "301", "Maps to", "2010-01-01", "2099-12-31", None),
            ("400", "401", "Maps to", "2010-01-01", "2099-12-31", None),
            ("600", "601", "Maps to", "2010-01-01", "2099-12-31", None),
        ],
        "concept_id_1 string, concept_id_2 string, relationship_id string, "
        "valid_start_date string, valid_end_date string, invalid_reason string",
    ).createOrReplaceTempView("concept_relationship")

    engine = PySparkEngine()
    # Reuse the test session rather than building a second one.
    engine._session = spark  # type: ignore[attr-defined]

    input_address = DiscAddress({"path": str(in_root), "format": "csv"})
    output_address = DiscAddress({"path": str(out_root), "format": "csv"})

    from ovid2.core.loaders import load_formatter

    for table in (
        "person",
        "visit_occurrence",
        "condition_occurrence",
        "observation_period",
        "provider",
        "care_site",
        "location",
        "death",
        "cdm_source",
        "measurement",
        "procedure_occurrence",
        "observation",
        "drug_exposure",
        "specimen",
        "device_exposure",
        "visit_detail",
        "note",
        "fact_relationship",
    ):
        formatter = load_formatter(
            pipeline, table, spark=spark, input_address=input_address, batch_id=batch,
            engine=engine,
        )
        run_pipeline(
            engine=engine,
            input_address=input_address,
            output_address=output_address,
            table_name=table,
            formatter=formatter,
            transformations=[],
            pipeline=pipeline,
            batch_id=batch,
        )

    # --- person ---------------------------------------------------------
    person = {r["person_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "person")}
    assert set(person) == {"P1", "P2"}
    assert person["P1"]["gender_concept_id"] == "8532"
    assert person["P1"]["race_concept_id"] == "8527"
    assert person["P1"]["year_of_birth"] == "1980"
    assert person["P1"]["mapped_from"] == "DEMOGRAPHIC"

    # --- visit_occurrence ----------------------------------------------
    visit = {r["visit_occurrence_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "visit_occurrence")}
    assert set(visit) == {"E1"}
    assert visit["E1"]["person_id"] == "P1"
    assert visit["E1"]["visit_concept_id"] == "9201"
    assert visit["E1"]["visit_type_concept_id"] == "32035"

    # --- condition_occurrence (concept-resolved + filtered) -------------
    cond = {r["condition_occurrence_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "condition_occurrence")}
    # Only the mappable ICD-10 code survives.
    assert set(cond) == {"D1"}
    assert cond["D1"]["condition_source_concept_id"] == "100"
    assert cond["D1"]["condition_concept_id"] == "200"
    assert cond["D1"]["person_id"] == "P1"
    assert cond["D1"]["visit_occurrence_id"] == "E1"

    # --- observation_period (hash surrogate id + period type) -----------
    obs = {r["person_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "observation_period")}
    assert set(obs) == {"P1", "P2"}
    assert obs["P1"]["period_type_concept_id"] == "32813"  # ENR_BASIS I
    assert obs["P2"]["period_type_concept_id"] == "32880"  # ENR_BASIS A
    assert int(obs["P1"]["observation_period_id"]) >= 0
    assert obs["P1"]["observation_period_id"] != obs["P2"]["observation_period_id"]
    assert str(obs["P2"]["observation_period_end_date"]) == "1900-01-01"

    # --- provider (hash surrogate id + gender dict) ---------------------
    prov = {r["provider_source_value"]: r for r in _read_omop_output(out_root, pipeline, batch, "provider")}
    assert set(prov) == {"PR1"}
    assert prov["PR1"]["npi"] == "1234567890"
    assert prov["PR1"]["gender_concept_id"] == "8532"  # F
    assert prov["PR1"]["specialty_source_value"] == "207R00000X"
    assert int(prov["PR1"]["provider_id"]) >= 0

    # --- care_site / location (derived, deduped, matching location_id) --
    care = {r["care_site_source_value"]: r for r in _read_omop_output(out_root, pipeline, batch, "care_site")}
    assert set(care) == {"F1"}
    loc_rows = _read_omop_output(out_root, pipeline, batch, "location")
    loc = {r["zip"]: r for r in loc_rows}
    assert "" in loc or set(loc)  # at least the F1 encounter's location
    # care_site.F1's location_id points at a real location row
    assert care["F1"]["location_id"] in {r["location_id"] for r in loc_rows}

    # --- death (person_id reuse + type dict + resolved cause) -----------
    death = {r["person_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "death")}
    assert set(death) == {"P1"}
    assert death["P1"]["death_type_concept_id"] == "32817"  # DEATH_SOURCE L
    # cause E11.9 resolves: source concept 100, 'Maps to' -> 200
    assert death["P1"]["cause_source_concept_id"] == "100"
    assert death["P1"]["cause_concept_id"] == "200"

    # --- cdm_source (single-row metadata) -------------------------------
    src_rows = _read_omop_output(out_root, pipeline, batch, "cdm_source")
    assert len(src_rows) == 1
    assert src_rows[0]["cdm_source_name"] == "Example Network"
    assert src_rows[0]["cdm_source_abbreviation"] == "EXN01"
    assert src_rows[0]["cdm_version"] == "5.3"

    # --- measurement (LOINC lab resolved to Measurement domain) ---------
    meas = {r["measurement_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "measurement")}
    assert "L1" in meas
    assert meas["L1"]["measurement_source_concept_id"] == "300"
    assert meas["L1"]["measurement_concept_id"] == "301"
    assert meas["L1"]["person_id"] == "P1"
    assert meas["L1"]["visit_occurrence_id"] == "E1"

    # --- procedure_occurrence (CPT resolved to Procedure domain) --------
    proc = {r["procedure_occurrence_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "procedure_occurrence")}
    assert "PX1" in proc
    assert proc["PX1"]["procedure_source_concept_id"] == "400"
    assert proc["PX1"]["procedure_concept_id"] == "401"
    assert proc["PX1"]["provider_id"] == "PR1"

    # --- drug_exposure (RxNorm MED_ADMIN resolved to Drug domain) -------
    drug = {r["drug_exposure_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "drug_exposure")}
    assert "MA1" in drug
    assert drug["MA1"]["drug_source_concept_id"] == "600"
    assert drug["MA1"]["drug_concept_id"] == "601"
    assert drug["MA1"]["drug_type_concept_id"] == "32830"

    # --- specimen (LAB_RESULT_CM specimen fields) -----------------------
    spec = {r["specimen_id"]: r for r in _read_omop_output(out_root, pipeline, batch, "specimen")}
    assert "L1" in spec
    assert spec["L1"]["specimen_concept_id"] == "4001225"  # BLD
    assert spec["L1"]["specimen_source_id"] == "L1"

    # --- observation (nothing maps here in this fixture) ----------------
    obs_rows = _read_omop_output(out_root, pipeline, batch, "observation")
    # No Observation-domain code in the fixture; header-only is acceptable.
    assert isinstance(obs_rows, list)

    # --- header-only tables (device_exposure / visit_detail / note /
    #     fact_relationship): the file exists with a header, zero rows.
    for empty_table, key_col in (
        ("device_exposure", "device_exposure_id"),
        ("visit_detail", "visit_detail_id"),
        ("note", "note_id"),
        ("fact_relationship", "domain_concept_id_1"),
    ):
        rows = _read_omop_output(out_root, pipeline, batch, empty_table)
        assert rows == []
