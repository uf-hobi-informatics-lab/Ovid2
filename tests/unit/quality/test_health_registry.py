"""Unit tests for the data-health registry / results / report (no Spark)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.transformations.quality import report as report_mod
from ovid2.transformations.quality.registry import (
    get_package,
    get_spec,
    latest_package,
    load_all_packages,
    load_all_specs,
    parts,
    select_specs,
)
from ovid2.transformations.quality.registry import (
    packages as check_packages,
)
from ovid2.transformations.quality.results import CheckOutcome, MetricRow

# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #


def test_load_all_specs_nonempty() -> None:
    specs = load_all_specs()
    assert specs
    assert all(s.operator for s in specs)


def test_specs_sorted_by_part_then_id() -> None:
    specs = load_all_specs()
    keys = [s.sort_key for s in specs]
    assert keys == sorted(keys)


def test_get_spec_by_id_and_name() -> None:
    by_id = get_spec("1.01")
    assert by_id is not None and by_id.name == "DC 1.01"
    by_name = get_spec("DC 1.01")
    assert by_name is not None and by_name.id == "1.01"
    assert get_spec("nope.nope") is None


def test_select_specs_by_part() -> None:
    part1 = select_specs(parts=["part1"], checks=None)
    assert part1
    assert all(s.part == "part1" for s in part1)


def test_select_specs_by_checks_overrides_parts() -> None:
    picked = select_specs(parts=["part3"], checks=["1.01"])
    assert [s.id for s in picked] == ["1.01"]


def test_parts_in_canonical_order() -> None:
    ps = parts()
    assert "part1" in ps
    # part1 sorts before integrity / mapping.
    assert ps.index("part1") < ps.index("integrity") if "integrity" in ps else True


# --------------------------------------------------------------------------- #
# Cycle-packages                                                               #
# --------------------------------------------------------------------------- #


def test_load_all_packages_nonempty() -> None:
    pkgs = load_all_packages()
    assert pkgs
    assert all(p.id and p.checks for p in pkgs)


def test_packages_ordered_most_recent_first() -> None:
    pkgs = load_all_packages()
    released = [p.released for p in pkgs]
    assert released == sorted(released, reverse=True)


def test_latest_package_is_first() -> None:
    pkgs = load_all_packages()
    assert latest_package() == pkgs[0].id


def test_get_package_by_id() -> None:
    pkg = get_package("20r1")
    assert pkg is not None
    assert pkg.id == "20r1"
    assert "1.01" in pkg.checks
    assert get_package("nope") is None


def test_packages_ids_helper() -> None:
    ids = check_packages()
    assert "20r1" in ids
    assert ids[0] == latest_package()


def test_20r1_is_curated_subset_of_current_checks() -> None:
    pkg = get_package("20r1")
    assert pkg is not None
    all_ids = {s.id for s in load_all_specs()}
    # 20r1 is the baseline cycle: a curated subset, every member of which
    # must still resolve to a registered check.
    assert set(pkg.checks) <= all_ids
    assert set(pkg.checks)  # non-empty


def test_dcq_v7_03_contains_full_edc_set() -> None:
    pkg = get_package("dcq_v7_03")
    assert pkg is not None
    all_ids = {s.id for s in load_all_specs()}
    # dcq_v7_03 (Cycle 20 / v7.03) is the full EDC port: it names every
    # EDC (numeric-id) check that ships today, plus the native panels.
    edc_ids = {i for i in all_ids if i[0].isdigit()}
    assert edc_ids <= set(pkg.checks), (
        "dcq_v7_03 is missing EDC checks: " + ", ".join(sorted(edc_ids - set(pkg.checks)))
    )
    assert set(pkg.checks) <= all_ids


def test_select_specs_by_package() -> None:
    picked = select_specs(packages=["20r1"])
    ids = {s.id for s in picked}
    pkg = get_package("20r1")
    assert pkg is not None
    assert ids == set(pkg.checks)


def test_select_specs_package_overrides_parts() -> None:
    # Package selection wins over parts (but loses to explicit checks).
    picked = select_specs(parts=["part1"], packages=["20r1"])
    assert {s.id for s in picked} == set(get_package("20r1").checks)  # type: ignore[union-attr]


def test_select_specs_checks_override_package() -> None:
    picked = select_specs(packages=["20r1"], checks=["1.01"])
    assert [s.id for s in picked] == ["1.01"]


def test_select_specs_unknown_package_selects_nothing() -> None:
    assert select_specs(packages=["does-not-exist"]) == []


# --------------------------------------------------------------------------- #
# Results model                                                                #
# --------------------------------------------------------------------------- #


def test_metric_row_as_table_row_order() -> None:
    row = MetricRow(
        check_id="1.01",
        check_name="DC_1_01",
        part="part1",
        edc_table="Table IID",
        source_table="HARVEST",
        metric="MISSING_REQUIRED_TABLE_FLAG",
        value_num=1.0,
        threshold_num=1.0,
        exception_flag=True,
        details={"required": True},
    )
    tup = row.as_table_row("RUN1")
    assert tup[0] == "RUN1"
    assert tup[1] == "1.01"
    assert tup[6] == "MISSING_REQUIRED_TABLE_FLAG"
    assert tup[10] is True
    # DETAILS serialised to JSON string.
    assert json.loads(tup[11]) == {"required": True}


def test_check_outcome_resolves_status() -> None:
    outcome = CheckOutcome(
        check_id="1.01", check_name="DC_1_01", part="part1", edc_table="", description="x"
    )
    outcome.rows = [
        MetricRow("1.01", "DC_1_01", "part1", "", "HARVEST", "M", exception_flag=True),
    ]
    outcome.resolve_status()
    assert outcome.status == "exception"
    assert outcome.exception_count == 1


def test_check_outcome_error_status_sticky() -> None:
    outcome = CheckOutcome(
        check_id="x", check_name="X", part="part1", edc_table="", description="x",
        status="error", error="boom",
    )
    outcome.resolve_status()
    assert outcome.status == "error"


# --------------------------------------------------------------------------- #
# Report artifact                                                              #
# --------------------------------------------------------------------------- #


def _outcomes() -> list[CheckOutcome]:
    passed = CheckOutcome(
        check_id="1.02", check_name="DC_1_02", part="part1", edc_table="", description="d"
    )
    exc = CheckOutcome(
        check_id="1.01", check_name="DC_1_01", part="part1", edc_table="", description="d"
    )
    exc.rows = [MetricRow("1.01", "DC_1_01", "part1", "", "HARVEST", "M", exception_flag=True)]
    exc.resolve_status()
    err = CheckOutcome(
        check_id="1.04", check_name="DC_1_04", part="part1", edc_table="", description="d",
        status="error", error="boom",
    )
    return [passed, exc, err]


def test_build_report_summary_counts() -> None:
    report = report_mod.build_report(
        instance_id="i", pipeline="demo", batch_id="b", cdm="pcornet_7_0",
        input_address="IN", output_address="OUT", outcomes=_outcomes(),
    )
    s = report["summary"]
    assert s["checks_total"] == 3
    assert s["checks_passed"] == 1
    assert s["checks_exception"] == 1
    assert s["checks_error"] == 1
    assert s["parts"]["part1"]["total"] == 3


def test_write_report_roundtrip(tmp_path: Path) -> None:
    report = report_mod.build_report(
        instance_id="i", pipeline="demo", batch_id="b", cdm="pcornet_7_0",
        input_address="IN", output_address="OUT", outcomes=_outcomes(),
    )
    path = report_mod.report_path(tmp_path, "i")
    report_mod.write_report(path, report)
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o644
    loaded = json.loads(path.read_text())
    assert loaded["summary"]["checks_total"] == 3


def test_report_validates_against_schema(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "src" / "ovid2" / "resources" / "schemas" / "health_report.v1.json"
    )
    schema = json.loads(schema_path.read_text())
    report = report_mod.build_report(
        instance_id="i", pipeline="demo", batch_id="b", cdm="pcornet_7_0",
        input_address="IN", output_address="OUT", outcomes=_outcomes(),
    )
    jsonschema.validate(report, schema)
