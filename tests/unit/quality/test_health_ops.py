"""Spark-backed unit tests for the data-health check operators + runner."""

from __future__ import annotations

from datetime import date

import pytest

from ovid2.cdm.registry import get_cdm
from ovid2.transformations.quality.ops import OpContext, get_operator
from ovid2.transformations.quality.registry import get_spec, select_specs
from ovid2.transformations.quality.runner import HealthCheckRunner


@pytest.fixture()
def cdm():
    return get_cdm("pcornet_7_0")


def _ctx(spark, cdm, available, *, run_date=None, prior_tables=None):
    return OpContext(
        session=spark,
        cdm=cdm,
        available_tables=frozenset(available),
        count_fn=lambda df: int(df.count()),
        pipeline="test",
        run_date=run_date,
        prior_tables=prior_tables,
    )


def _register(spark, name, rows, cols):
    df = spark.createDataFrame(rows, cols)
    df.createOrReplaceTempView(name)


def test_table_present_flags_missing_required(spark, cdm) -> None:
    _register(spark, "DEMOGRAPHIC", [("P1", "M")], ["PATID", "SEX"])
    spec = get_spec("1.01")
    rows = get_operator("table_present")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    # HARVEST is required and absent -> exception.
    harvest = [r for r in rows if r.source_table == "HARVEST" and r.exception_flag]
    assert harvest and harvest[0].metric == "MISSING_REQUIRED_TABLE_FLAG"
    # DEMOGRAPHIC exists -> its exists flag is 1.
    demo = [r for r in rows if r.source_table == "DEMOGRAPHIC" and r.metric == "TABLE_EXISTS_FLAG"]
    assert demo and demo[0].value_num == 1.0


def test_pk_uniqueness_detects_duplicate(spark, cdm) -> None:
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", "M"), ("P1", "F"), ("P2", "M")],
        ["PATID", "SEX"],
    )
    spec = get_spec("health.pk_uniqueness")
    rows = get_operator("pk_uniqueness")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    dup = [r for r in rows if r.metric == "DUPLICATE_PK_COUNT"]
    assert dup and dup[0].value_num == 1.0 and dup[0].exception_flag


def test_valueset_membership_flags_out_of_set(spark, cdm) -> None:
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", "M"), ("P2", "F"), ("P3", "ZZ"), ("P4", "M")],
        ["PATID", "SEX"],
    )
    spec = get_spec("health.valueset_conformance")
    rows = get_operator("valueset_membership")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    pct = [r for r in rows if r.metric == "OUT_OF_VALUESET_PCT"]
    assert pct
    # 1 of 4 SEX values ('ZZ') is out of the valueset = 25%.
    assert pytest.approx(pct[0].value_num, abs=0.01) == 25.0
    assert pct[0].exception_flag


def test_dc_1_06_wildcard_prefix_conforms(spark, cdm) -> None:
    """DC 1.06: UD_* / PC_* OBSGEN_TYPE values are prefix-matched, not literal.

    Before the wildcard fix the operator compared data against the literal
    string 'UD_*'; a real value like 'UD_FOO' was wrongly flagged. With the
    fix, any value whose 3-char prefix is 'UD_' or 'PC_' conforms.
    """
    _register(
        spark,
        "OBS_GEN",
        [
            ("O1", "UD_CUSTOM_A"),  # user-defined -> conforms via UD_ prefix
            ("O2", "PC_RESERVED"),  # PCORnet reserved -> conforms via PC_ prefix
            ("O3", "RX"),  # exact valueset member
            ("O4", "ZZZ"),  # genuinely out of valueset -> the only bad row
        ],
        ["OBSGENID", "OBSGEN_TYPE"],
    )
    spec = get_spec("1.06")
    rows = get_operator("valueset_membership")(_ctx(spark, cdm, {"OBS_GEN"}), spec)
    oo = [
        r
        for r in rows
        if r.metric == "OUT_OF_VALUESET_ROWS"
        and r.details
        and r.details.get("column") == "OBSGEN_TYPE"
    ]
    assert oo, "expected an OBSGEN_TYPE metric row"
    # Only 'ZZZ' is out of set; UD_*/PC_* prefixes conform.
    assert oo[0].value_num == 1.0
    pct = [
        r
        for r in rows
        if r.metric == "OUT_OF_VALUESET_PCT"
        and r.details
        and r.details.get("column") == "OBSGEN_TYPE"
    ]
    assert pct and pct[0].exception_flag  # any occurrence flags (threshold 0)
    assert "UD_" in pct[0].details["wildcard_prefixes"]
    assert "PC_" in pct[0].details["wildcard_prefixes"]


def test_dc_1_06_case_sensitive(spark, cdm) -> None:
    """DC 1.06 matches case-sensitively: a lowercase code is out-of-valueset.

    SEX valueset codes are uppercase (A/F/M/...). A lowercase 'm' does not
    match under the SAS case-sensitive $format, so it must be flagged.
    """
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", "M"), ("P2", "F"), ("P3", "m")],
        ["PATID", "SEX"],
    )
    spec = get_spec("1.06")
    rows = get_operator("valueset_membership")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    oo = [
        r
        for r in rows
        if r.metric == "OUT_OF_VALUESET_ROWS" and r.details and r.details.get("column") == "SEX"
    ]
    assert oo and oo[0].value_num == 1.0  # lowercase 'm' is out of set


def test_future_date_flags(spark, cdm) -> None:
    _register(
        spark,
        "ENCOUNTER",
        [("E1", "P1", "2020-01-01"), ("E2", "P2", "2999-12-31")],
        ["ENCOUNTERID", "PATID", "ADMIT_DATE"],
    )
    spec = get_spec("health.future_date")
    rows = get_operator("future_date")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    fut = [r for r in rows if r.metric == "FUTURE_DATE_ROWS"]
    assert fut and fut[0].value_num == 1.0 and fut[0].exception_flag


def test_orphan_fk_detects_dangling(spark, cdm) -> None:
    _register(spark, "DEMOGRAPHIC", [("P1",), ("P2",)], ["PATID"])
    _register(
        spark,
        "ENCOUNTER",
        [("E1", "P1"), ("E2", "P9")],
        ["ENCOUNTERID", "PATID"],
    )
    spec = get_spec("health.orphan_fk")
    rows = get_operator("orphan_fk")(_ctx(spark, cdm, {"DEMOGRAPHIC", "ENCOUNTER"}), spec)
    orphan = [r for r in rows if r.metric == "ORPHAN_ROWS" and r.source_table == "ENCOUNTER"]
    assert orphan and orphan[0].value_num == 1.0 and orphan[0].exception_flag


def test_null_rate(spark, cdm) -> None:
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", None), ("P2", None), ("P3", "M"), ("P4", None)],
        ["PATID", "SEX"],
    )
    spec = get_spec("health.null_rate")
    rows = get_operator("null_rate")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    sex = [r for r in rows if r.details and r.details.get("column") == "SEX"]
    assert sex and pytest.approx(sex[0].value_num, abs=0.01) == 75.0


def test_runner_end_to_end_status_and_dataframe(spark, cdm) -> None:
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", "M"), ("P1", "F"), ("P3", "ZZ")],
        ["PATID", "SEX"],
    )
    runner = HealthCheckRunner(
        session=spark,
        cdm=cdm,
        pipeline="test",
        batch_id="b1",
        instance_id="20260101-000000-abcdef",
    )
    specs = [
        s
        for s in select_specs(parts=None, checks=None)
        if s.id in ("1.01", "health.pk_uniqueness", "health.valueset_conformance")
    ]
    outcomes = runner.run(specs, available_tables=frozenset({"DEMOGRAPHIC"}))
    by_id = {o.check_id: o for o in outcomes}
    assert by_id["health.pk_uniqueness"].status == "exception"  # duplicate PATID
    assert by_id["health.valueset_conformance"].status == "exception"  # ZZ out of SEX valueset
    df = runner.build_results_dataframe(outcomes)
    assert df.count() > 0
    assert df.columns[0] == "RUN_ID"


# --------------------------------------------------------------------------- #
# EDC v7.03 ports — new operators (Phase 2 verification)                       #
# --------------------------------------------------------------------------- #


def _metric(rows, metric, **match):
    """Return the single metric row matching ``metric`` (+ optional detail keys)."""
    out = []
    for r in rows:
        if r.metric != metric:
            continue
        if match and (not r.details or any(r.details.get(k) != v for k, v in match.items())):
            continue
        out.append(r)
    return out


# --- refresh_date_present (DC 1.18) --------------------------------------- #


def test_refresh_date_present_flags_table_without_parseable_date(spark, cdm) -> None:
    # BIRTH_DATE present as a column but never parseable -> refresh date is N/A.
    _register(spark, "DEMOGRAPHIC", [("P1", "not-a-date")], ["PATID", "BIRTH_DATE"])
    spec = get_spec("1.18")
    rows = get_operator("refresh_date_present")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    flag = _metric(rows, "MISSING_REFRESH_DATE_FLAG")
    assert flag and flag[0].value_num == 1.0 and flag[0].exception_flag


def test_refresh_date_present_passes_when_date_populated(spark, cdm) -> None:
    _register(spark, "ENCOUNTER", [("E1", "2020-01-01")], ["ENCOUNTERID", "ADMIT_DATE"])
    spec = get_spec("1.18")
    rows = get_operator("refresh_date_present")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    flag = _metric(rows, "MISSING_REFRESH_DATE_FLAG")
    assert flag and flag[0].value_num == 0.0 and not flag[0].exception_flag


# --- panel_loinc_rate (DC 1.20) ------------------------------------------- #


def test_panel_loinc_rate_flags_when_panels_exceed_5pct(spark, cdm) -> None:
    # 2 panel LOINCs out of 4 -> 50% > 5%.
    _register(
        spark,
        "LAB_RESULT_CM",
        [("24323-8",), ("24356-8",), ("11111-1",), ("22222-2",)],
        ["LAB_LOINC"],
    )
    spec = get_spec("1.20")
    rows = get_operator("panel_loinc_rate")(_ctx(spark, cdm, {"LAB_RESULT_CM"}), spec)
    pct = _metric(rows, "PANEL_LOINC_PCT")
    assert pct and pytest.approx(pct[0].value_num, abs=0.01) == 50.0
    assert pct[0].exception_flag


def test_panel_loinc_rate_passes_when_few_panels(spark, cdm) -> None:
    rows_in = [("11111-1",)] * 99 + [("24323-8",)]  # 1% panels
    _register(spark, "LAB_RESULT_CM", rows_in, ["LAB_LOINC"])
    spec = get_spec("1.20")
    rows = get_operator("panel_loinc_rate")(_ctx(spark, cdm, {"LAB_RESULT_CM"}), spec)
    pct = _metric(rows, "PANEL_LOINC_PCT")
    assert pct and not pct[0].exception_flag


# --- code_placement (DC 1.13) --------------------------------------------- #


def test_code_placement_flags_bad_code_type(spark, cdm) -> None:
    # DX_TYPE valid types are 09/10/11/SM. 'ZZ' is bad -> 50% > 5%.
    _register(spark, "DIAGNOSIS", [("A", "10"), ("B", "ZZ")], ["DX", "DX_TYPE"])
    spec = get_spec("1.13")
    rows = get_operator("code_placement")(_ctx(spark, cdm, {"DIAGNOSIS"}), spec)
    pct = _metric(rows, "BAD_CODE_TYPE_PCT")
    assert pct and pytest.approx(pct[0].value_num, abs=0.01) == 50.0
    assert pct[0].exception_flag


def test_code_placement_passes_when_all_valid(spark, cdm) -> None:
    _register(spark, "DIAGNOSIS", [("A", "10"), ("B", "09")], ["DX", "DX_TYPE"])
    spec = get_spec("1.13")
    rows = get_operator("code_placement")(_ctx(spark, cdm, {"DIAGNOSIS"}), spec)
    pct = _metric(rows, "BAD_CODE_TYPE_PCT")
    assert pct and pct[0].value_num == 0.0 and not pct[0].exception_flag


# --- illogical_date (DC 2.03) --------------------------------------------- #


def test_illogical_date_flags_discharge_before_admit(spark, cdm) -> None:
    _register(
        spark,
        "ENCOUNTER",
        [("E1", "2020-01-10", "2020-01-05"), ("E2", "2020-02-01", "2020-02-03")],
        ["ENCOUNTERID", "ADMIT_DATE", "DISCHARGE_DATE"],
    )
    spec = get_spec("2.03")
    rows = get_operator("illogical_date")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    enc = _metric(rows, "ILLOGICAL_DATE_PCT")
    assert enc and pytest.approx(enc[0].value_num, abs=0.01) == 50.0
    assert enc[0].exception_flag


def test_illogical_date_passes_when_ordered(spark, cdm) -> None:
    _register(
        spark,
        "ENCOUNTER",
        [("E1", "2020-01-01", "2020-01-05")],
        ["ENCOUNTERID", "ADMIT_DATE", "DISCHARGE_DATE"],
    )
    spec = get_spec("2.03")
    rows = get_operator("illogical_date")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    enc = _metric(rows, "ILLOGICAL_DATE_PCT")
    assert enc and enc[0].value_num == 0.0 and not enc[0].exception_flag


# --- implausible_value (DC 2.02) ------------------------------------------ #


def test_implausible_value_flags_out_of_range(spark, cdm) -> None:
    # HT range is 0..96. Two of four values are out of range -> 50% > 10%.
    _register(
        spark,
        "VITAL",
        [("70",), ("60",), ("500",), ("-3",)],
        ["HT"],
    )
    spec = get_spec("2.02")
    rows = get_operator("implausible_value")(_ctx(spark, cdm, {"VITAL"}), spec)
    pct = [
        r
        for r in _metric(rows, "IMPLAUSIBLE_VALUE_PCT")
        if r.details and r.details.get("column") == "HT"
    ]
    assert pct and pytest.approx(pct[0].value_num, abs=0.01) == 50.0
    assert pct[0].exception_flag


def test_implausible_value_passes_within_range(spark, cdm) -> None:
    _register(spark, "VITAL", [("70",), ("60",), ("55",)], ["HT"])
    spec = get_spec("2.02")
    rows = get_operator("implausible_value")(_ctx(spark, cdm, {"VITAL"}), spec)
    pct = [
        r
        for r in _metric(rows, "IMPLAUSIBLE_VALUE_PCT")
        if r.details and r.details.get("column") == "HT"
    ]
    assert pct and pct[0].value_num == 0.0 and not pct[0].exception_flag


# --- encounter_per_visit (DC 2.04) ---------------------------------------- #


def test_encounter_per_visit_flags_visit_splitting(spark, cdm) -> None:
    # 3 IP encounters that collapse to 1 distinct visit -> ratio 3 > 2.
    _register(
        spark,
        "ENCOUNTER",
        [
            ("E1", "P1", "2020-01-01", "IP"),
            ("E2", "P1", "2020-01-01", "IP"),
            ("E3", "P1", "2020-01-01", "IP"),
        ],
        ["ENCOUNTERID", "PATID", "ADMIT_DATE", "ENC_TYPE"],
    )
    spec = get_spec("2.04")
    rows = get_operator("encounter_per_visit")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    ip = [
        r
        for r in _metric(rows, "ENCOUNTERS_PER_VISIT")
        if r.details and r.details.get("enc_type") == "IP"
    ]
    assert ip and pytest.approx(ip[0].value_num, abs=0.01) == 3.0
    assert ip[0].exception_flag


def test_encounter_per_visit_passes_one_per_visit(spark, cdm) -> None:
    _register(
        spark,
        "ENCOUNTER",
        [
            ("E1", "P1", "2020-01-01", "IP"),
            ("E2", "P2", "2020-02-01", "IP"),
        ],
        ["ENCOUNTERID", "PATID", "ADMIT_DATE", "ENC_TYPE"],
    )
    spec = get_spec("2.04")
    rows = get_operator("encounter_per_visit")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    ip = [
        r
        for r in _metric(rows, "ENCOUNTERS_PER_VISIT")
        if r.details and r.details.get("enc_type") == "IP"
    ]
    assert ip and ip[0].value_num == 1.0 and not ip[0].exception_flag


# --- records_per_encounter (DC 3.01 / 3.02) ------------------------------- #


def test_records_per_encounter_flags_low_capture(spark, cdm) -> None:
    # 2 IP encounters, 0 procedures -> 0.0 < 1.0 minimum.
    _register(
        spark,
        "ENCOUNTER",
        [("E1", "IP"), ("E2", "IP")],
        ["ENCOUNTERID", "ENC_TYPE"],
    )
    _register(spark, "PROCEDURES", [("E9", "10")], ["ENCOUNTERID", "PX_TYPE"])
    spec = get_spec("3.02")
    rows = get_operator("records_per_encounter")(
        _ctx(spark, cdm, {"ENCOUNTER", "PROCEDURES"}), spec
    )
    ip = [
        r
        for r in _metric(rows, "RECORDS_PER_ENCOUNTER")
        if r.details and r.details.get("enc_type") == "IP"
    ]
    assert ip and ip[0].value_num == 0.0 and ip[0].exception_flag


def test_records_per_encounter_passes_sufficient_capture(spark, cdm) -> None:
    _register(spark, "ENCOUNTER", [("E1", "IP")], ["ENCOUNTERID", "ENC_TYPE"])
    _register(
        spark,
        "PROCEDURES",
        [("E1", "10"), ("E1", "09")],
        ["ENCOUNTERID", "PX_TYPE"],
    )
    spec = get_spec("3.02")
    rows = get_operator("records_per_encounter")(
        _ctx(spark, cdm, {"ENCOUNTER", "PROCEDURES"}), spec
    )
    ip = [
        r
        for r in _metric(rows, "RECORDS_PER_ENCOUNTER")
        if r.details and r.details.get("enc_type") == "IP"
    ]
    assert ip and ip[0].value_num == 2.0 and not ip[0].exception_flag


# --- field_completeness (DC 3.03) ----------------------------------------- #


def test_field_completeness_flags_missing_key_field(spark, cdm) -> None:
    # SEX missing in 2 of 4 -> 50% >= 10%.
    _register(
        spark,
        "DEMOGRAPHIC",
        [
            ("P1", "M", "2000-01-01"),
            ("P2", None, "2000-01-01"),
            ("P3", None, "2000-01-01"),
            ("P4", "F", "2000-01-01"),
        ],
        ["PATID", "SEX", "BIRTH_DATE"],
    )
    spec = get_spec("3.03")
    rows = get_operator("field_completeness")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    sex = [
        r for r in _metric(rows, "MISSING_PCT") if r.details and r.details.get("column") == "SEX"
    ]
    assert sex and pytest.approx(sex[0].value_num, abs=0.01) == 50.0
    assert sex[0].exception_flag


def test_field_completeness_passes_when_populated(spark, cdm) -> None:
    _register(
        spark,
        "DEMOGRAPHIC",
        [("P1", "M", "2000-01-01"), ("P2", "F", "2000-01-01")],
        ["PATID", "SEX", "BIRTH_DATE"],
    )
    spec = get_spec("3.03")
    rows = get_operator("field_completeness")(_ctx(spark, cdm, {"DEMOGRAPHIC"}), spec)
    sex = [
        r for r in _metric(rows, "MISSING_PCT") if r.details and r.details.get("column") == "SEX"
    ]
    assert sex and sex[0].value_num == 0.0 and not sex[0].exception_flag


# --- known_value_coverage (DC 3.08 …) ------------------------------------- #


def test_known_value_coverage_flags_low_coverage(spark, cdm) -> None:
    # 1 of 4 MEDADMIN_CODE populated -> 25% < 80%.
    _register(
        spark,
        "MED_ADMIN",
        [("111",), (None,), (None,), (None,)],
        ["MEDADMIN_CODE"],
    )
    spec = get_spec("3.15")
    rows = get_operator("known_value_coverage")(_ctx(spark, cdm, {"MED_ADMIN"}), spec)
    cov = _metric(rows, "COVERAGE_PCT")
    assert cov and pytest.approx(cov[0].value_num, abs=0.01) == 25.0
    assert cov[0].exception_flag


def test_known_value_coverage_passes_high_coverage(spark, cdm) -> None:
    _register(
        spark,
        "MED_ADMIN",
        [("111",), ("222",), ("333",), ("444",)],
        ["MEDADMIN_CODE"],
    )
    spec = get_spec("3.15")
    rows = get_operator("known_value_coverage")(_ctx(spark, cdm, {"MED_ADMIN"}), spec)
    cov = _metric(rows, "COVERAGE_PCT")
    assert cov and cov[0].value_num == 100.0 and not cov[0].exception_flag


# --- patient_coverage (DC 2.09 …) ----------------------------------------- #


def test_patient_coverage_flags_low_related_coverage(spark, cdm) -> None:
    # 2 encounter patients, only 1 also in DIAGNOSIS -> 50% < 80%.
    _register(spark, "ENCOUNTER", [("E1", "P1"), ("E2", "P2")], ["ENCOUNTERID", "PATID"])
    _register(spark, "DIAGNOSIS", [("D1", "P1")], ["DIAGNOSISID", "PATID"])
    _register(spark, "VITAL", [("V1", "P1"), ("V2", "P2")], ["VITALID", "PATID"])
    spec = get_spec("2.09")
    rows = get_operator("patient_coverage")(
        _ctx(spark, cdm, {"ENCOUNTER", "DIAGNOSIS", "VITAL"}), spec
    )
    dx = [r for r in _metric(rows, "PATIENT_COVERAGE_PCT") if r.source_table == "DIAGNOSIS"]
    assert dx and pytest.approx(dx[0].value_num, abs=0.01) == 50.0
    assert dx[0].exception_flag


def test_patient_coverage_passes_full_coverage(spark, cdm) -> None:
    _register(spark, "ENCOUNTER", [("E1", "P1")], ["ENCOUNTERID", "PATID"])
    _register(spark, "DIAGNOSIS", [("D1", "P1")], ["DIAGNOSISID", "PATID"])
    _register(spark, "VITAL", [("V1", "P1")], ["VITALID", "PATID"])
    spec = get_spec("2.09")
    rows = get_operator("patient_coverage")(
        _ctx(spark, cdm, {"ENCOUNTER", "DIAGNOSIS", "VITAL"}), spec
    )
    dx = [r for r in _metric(rows, "PATIENT_COVERAGE_PCT") if r.source_table == "DIAGNOSIS"]
    assert dx and dx[0].value_num == 100.0 and not dx[0].exception_flag


# --- bad_zip (DC 1.17) ---------------------------------------------------- #


def test_bad_zip_flags_nonconforming(spark, cdm) -> None:
    """DC 1.17: alpha chars or wrong length flag; any occurrence is an exception."""
    _register(
        spark,
        "LDS_ADDRESS_HISTORY",
        [
            ("A1", "32611"),  # valid 5-digit
            ("A2", "3261"),  # too short -> bad
            ("A3", "3261X"),  # alpha char -> bad
            ("A4", ""),  # blank -> missing, NOT bad
        ],
        ["ADDRESSID", "ADDRESS_ZIP5"],
    )
    spec = get_spec("1.17")
    rows = get_operator("bad_zip")(_ctx(spark, cdm, {"LDS_ADDRESS_HISTORY"}), spec)
    z5 = [
        r
        for r in rows
        if r.metric == "BAD_ZIP_ROWS" and r.details and r.details.get("column") == "ADDRESS_ZIP5"
    ]
    assert z5 and z5[0].value_num == 2.0  # '3261' + '3261X'
    assert z5[0].exception_flag


def test_bad_zip_passes_all_conforming(spark, cdm) -> None:
    _register(
        spark,
        "LDS_ADDRESS_HISTORY",
        [("A1", "326110001"), ("A2", "326115678")],
        ["ADDRESSID", "ADDRESS_ZIP9"],
    )
    spec = get_spec("1.17")
    rows = get_operator("bad_zip")(_ctx(spark, cdm, {"LDS_ADDRESS_HISTORY"}), spec)
    z9 = [
        r
        for r in rows
        if r.metric == "BAD_ZIP_ROWS" and r.details and r.details.get("column") == "ADDRESS_ZIP9"
    ]
    assert z9 and z9[0].value_num == 0.0 and not z9[0].exception_flag


# --- rxnorm_tier_coverage (DC 3.08) --------------------------------------- #


def test_rxnorm_tier_coverage_flags_low_tier1(spark, cdm) -> None:
    """DC 3.08: <80% Tier 1 RxNorm coverage flags an exception.

    CUI 89904 is a Tier 1 (SCD) code; CUI 38 is Tier 2 in the packaged
    reference. 1 of 4 records maps to Tier 1 = 25% < 80% -> exception.
    """
    _register(
        spark,
        "PRESCRIBING",
        [
            ("R1", "89904"),  # Tier 1
            ("R2", "38"),  # Tier 2
            ("R3", "38"),  # Tier 2
            ("R4", "99999999"),  # not in reference -> not Tier 1
        ],
        ["PRESCRIBINGID", "RXNORM_CUI"],
    )
    spec = get_spec("3.08")
    rows = get_operator("rxnorm_tier_coverage")(_ctx(spark, cdm, {"PRESCRIBING"}), spec)
    cov = [r for r in rows if r.metric == "TIER1_COVERAGE_PCT"]
    assert cov and pytest.approx(cov[0].value_num, abs=0.01) == 25.0
    assert cov[0].exception_flag


def test_rxnorm_tier_coverage_passes_high_tier1(spark, cdm) -> None:
    _register(
        spark,
        "PRESCRIBING",
        [("R1", "89904"), ("R2", "89904"), ("R3", "89904"), ("R4", "38")],
        ["PRESCRIBINGID", "RXNORM_CUI"],
    )
    spec = get_spec("3.08")
    rows = get_operator("rxnorm_tier_coverage")(_ctx(spark, cdm, {"PRESCRIBING"}), spec)
    cov = [r for r in rows if r.metric == "TIER1_COVERAGE_PCT"]
    # 3 of 4 = 75% ... still < 80% -> exception; use all-Tier1 for a pass case.
    assert cov
    _register(
        spark,
        "PRESCRIBING",
        [("R1", "89904"), ("R2", "89904"), ("R3", "89904"), ("R4", "89904")],
        ["PRESCRIBINGID", "RXNORM_CUI"],
    )
    rows2 = get_operator("rxnorm_tier_coverage")(_ctx(spark, cdm, {"PRESCRIBING"}), spec)
    cov2 = [r for r in rows2 if r.metric == "TIER1_COVERAGE_PCT"]
    assert cov2 and cov2[0].value_num == 100.0 and not cov2[0].exception_flag


# --- monthly_volume (DC 2.08 …) ------------------------------------------- #


# ``monthly_volume`` runs a deliberate ``.collect()`` analysis pass (the tiny
# per-month bucket frame, marked with the OVID2001 noqa marker in the operator). Under
# PySpark's py4j teardown that collect can leave a briefly-unclosed socket
# whose ``ResourceWarning`` the project-wide ``filterwarnings=error`` would
# otherwise escalate. The warning is a JVM-bridge teardown artefact unrelated
# to the check logic, so we ignore it on these two tests only.
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_monthly_volume_flags_recent_month_drop(spark, cdm) -> None:
    # 12 months of ~10 rows, then a most-recent month of 1 -> below 75% floor.
    data = []
    for m in range(1, 13):
        for _ in range(10):
            data.append((f"2020-{m:02d}-15",))
    data.append(("2021-01-15",))  # most-recent month, single row
    _register(spark, "VITAL", data, ["MEASURE_DATE"])
    spec = get_spec("3.11")  # DC 3.11 uses the simple monthly_volume operator
    rows = get_operator("monthly_volume")(_ctx(spark, cdm, {"VITAL"}), spec)
    recent = [r for r in _metric(rows, "RECENT_MONTH_ROWS") if r.source_table == "VITAL"]
    assert recent and recent[0].value_num == 1.0 and recent[0].exception_flag


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_monthly_volume_passes_stable_volume(spark, cdm) -> None:
    data = []
    for m in range(1, 13):
        for _ in range(10):
            data.append((f"2020-{m:02d}-15",))
    for _ in range(10):
        data.append(("2021-01-15",))  # most-recent month matches the benchmark
    _register(spark, "VITAL", data, ["MEASURE_DATE"])
    spec = get_spec("3.11")
    rows = get_operator("monthly_volume")(_ctx(spark, cdm, {"VITAL"}), spec)
    recent = [r for r in _metric(rows, "RECENT_MONTH_ROWS") if r.source_table == "VITAL"]
    assert recent and recent[0].value_num == 10.0 and not recent[0].exception_flag


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_monthly_volume_zscore_flags_collapse_month(spark, cdm) -> None:
    """DC 2.08: a month that collapses to near-zero after a stable >=500 benchmark flags.

    Build ~1000 rows/month (with small monthly variation so lag_std>0) for two
    years on ENCOUNTER (ENC_TYPE=IP), then a collapse month, then a normal tail
    so the collapse month is not excluded by the month11_date window. The
    collapse falls >7 SDs below the lagged 12-month mean and flags.
    """
    data = []
    counter = 0

    def _month(year: int, m: int, n: int) -> None:
        nonlocal counter
        for _ in range(n):
            counter += 1
            data.append((f"{year}-{m:02d}-15", "IP", f"E{counter}"))

    for year in (2021, 2022):
        for m in range(1, 13):
            _month(year, m, 1000 + (m % 5) * 20)  # small variation -> lag_std>0
    _month(2023, 1, 1)  # collapse month
    for m in range(2, 13):  # normal tail (2023-02 .. 2023-12)
        _month(2023, m, 1000 + (m % 5) * 20)
    for m in range(1, 13):  # a full extra year so 2023-01 is outside month11
        _month(2024, m, 1000 + (m % 5) * 20)
    _register(spark, "ENCOUNTER", data, ["ADMIT_DATE", "ENC_TYPE", "ENCOUNTERID"])
    spec = get_spec("2.08")
    rows = get_operator("monthly_volume_zscore")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    enc = [
        r for r in rows if r.metric == "MONTHLY_VOLUME_OUTLIER" and r.source_table == "ENCOUNTER"
    ]
    assert enc and enc[0].exception_flag
    months = [o["month"] for o in enc[0].details["outliers"]]
    assert "2023-01" in months


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_monthly_volume_zscore_passes_stable(spark, cdm) -> None:
    """DC 2.08: a stable >=500/month series raises no outlier."""
    data = []
    counter = 0
    for year in (2021, 2022, 2023):
        for m in range(1, 13):
            for _ in range(1000 + (m % 5) * 20):
                counter += 1
                data.append((f"{year}-{m:02d}-15", "IP", f"E{counter}"))
    _register(spark, "ENCOUNTER", data, ["ADMIT_DATE", "ENC_TYPE", "ENCOUNTERID"])
    spec = get_spec("2.08")
    rows = get_operator("monthly_volume_zscore")(_ctx(spark, cdm, {"ENCOUNTER"}), spec)
    enc = [
        r for r in rows if r.metric == "MONTHLY_VOLUME_OUTLIER" and r.source_table == "ENCOUNTER"
    ]
    assert enc and not enc[0].exception_flag


# --- refresh_trend (DC 4.01 …) -------------------------------------------- #


def test_refresh_trend_flags_drop_to_zero(spark, cdm) -> None:
    from pyspark.sql.types import StringType, StructField, StructType

    empty_schema = StructType([StructField("PATID", StringType(), True)])
    spark.createDataFrame([], empty_schema).createOrReplaceTempView("DEMOGRAPHIC")  # current = 0
    prior = spark.createDataFrame([("P1",), ("P2",)], ["PATID"])
    spec = get_spec("4.01")
    ctx = _ctx(spark, cdm, {"DEMOGRAPHIC"}, prior_tables={"DEMOGRAPHIC": prior})
    rows = get_operator("refresh_trend")(ctx, spec)
    delta = _metric(rows, "RECORD_DELTA_PCT")
    assert delta and delta[0].exception_flag
    assert delta[0].details and delta[0].details.get("dropped_to_zero") is True


def test_refresh_trend_flags_large_decrease(spark, cdm) -> None:
    _register(spark, "DEMOGRAPHIC", [("P1",)], ["PATID"])  # current = 1
    prior = spark.createDataFrame([("P1",), ("P2",), ("P3",), ("P4",)], ["PATID"])  # 4 -> -75%
    spec = get_spec("4.01")
    ctx = _ctx(spark, cdm, {"DEMOGRAPHIC"}, prior_tables={"DEMOGRAPHIC": prior})
    rows = get_operator("refresh_trend")(ctx, spec)
    delta = _metric(rows, "RECORD_DELTA_PCT")
    assert delta and pytest.approx(delta[0].value_num, abs=0.01) == -75.0
    assert delta[0].exception_flag


def test_refresh_trend_passes_stable_count(spark, cdm) -> None:
    _register(spark, "DEMOGRAPHIC", [("P1",), ("P2",)], ["PATID"])
    prior = spark.createDataFrame([("P1",), ("P2",)], ["PATID"])
    spec = get_spec("4.01")
    ctx = _ctx(spark, cdm, {"DEMOGRAPHIC"}, prior_tables={"DEMOGRAPHIC": prior})
    rows = get_operator("refresh_trend")(ctx, spec)
    delta = _metric(rows, "RECORD_DELTA_PCT")
    assert delta and delta[0].value_num == 0.0 and not delta[0].exception_flag


# --- extended orphan_fk pct-threshold path (DC 1.09) ---------------------- #


def test_orphan_fk_pct_threshold_flags_above_5pct(spark, cdm) -> None:
    # 20 encounter rows, 2 orphaned encounters -> 10% > 5%.
    _register(spark, "DEMOGRAPHIC", [("P1",)], ["PATID"])
    enc = [(f"E{i}", "P1", "V1") for i in range(18)]
    enc += [("E18", "P1", "V9"), ("E19", "P1", "V8")]  # 2 orphan ENCOUNTERID children
    _register(spark, "ENCOUNTER", [(e[0], e[1]) for e in enc], ["ENCOUNTERID", "PATID"])
    # A child table referencing ENCOUNTER via ENCOUNTERID with 2 orphans:
    child = [(f"D{i}", f"E{i}") for i in range(18)]
    child += [("D18", "E999"), ("D19", "E998")]
    _register(spark, "DIAGNOSIS", child, ["DIAGNOSISID", "ENCOUNTERID"])
    spec = get_spec("1.09")
    rows = get_operator("orphan_fk")(
        _ctx(spark, cdm, {"DEMOGRAPHIC", "ENCOUNTER", "DIAGNOSIS"}), spec
    )
    pct = [r for r in _metric(rows, "ORPHAN_PCT") if r.source_table == "DIAGNOSIS"]
    assert pct and pytest.approx(pct[0].value_num, abs=0.01) == 10.0
    assert pct[0].exception_flag


# --- extended future_date pct-threshold path (DC 2.01) -------------------- #


def test_future_date_pct_threshold(spark, cdm) -> None:
    # 1 of 10 rows in the future -> 10% > 5% pct threshold.
    data = [(f"E{i}", "2020-01-01") for i in range(9)] + [("E9", "2999-12-31")]
    _register(spark, "ENCOUNTER", data, ["ENCOUNTERID", "ADMIT_DATE"])
    spec = get_spec("2.01")
    rows = get_operator("future_date")(
        _ctx(spark, cdm, {"ENCOUNTER"}, run_date=date(2024, 1, 1)), spec
    )
    pct = _metric(rows, "FUTURE_DATE_PCT")
    assert pct and pytest.approx(pct[0].value_num, abs=0.01) == 10.0
    assert pct[0].exception_flag
