"""Unit tests — field-level pipeline lineage (navigator backend).

Covers :mod:`ovid2.web.services.pipeline_lineage_svc` and its
``/api/v1/navigator`` + ``/api/v1/pipeline-packages/{p}/lineage``
endpoints against the shipped ``example_pcornet`` / ``acme_from_disc`` /
``site`` packages (read-only) and a throwaway clone carrying transform
assignments, a pipeline-private chain and a ``derive_column`` UDF.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ovid2.config.column_roles import resolve_roles
from ovid2.web.services import pipeline_lineage_svc as lineage

pytestmark = pytest.mark.unit


def _installed(name: str) -> bool:
    """Whether ``ovid2_pipelines.<name>`` exists (site packages are absent from the public mirror)."""
    return importlib.util.find_spec(f"ovid2_pipelines.{name}") is not None


#: The legacy-parity port and the semantic-join site package used below.
HAS_LEGACY = _installed("site")
needs_legacy = pytest.mark.skipif(not HAS_LEGACY, reason="legacy-parity site port not installed")
needs_acme = pytest.mark.skipif(
    not _installed("acme_from_disc"), reason="semantic-join site package not installed"
)


@pytest.fixture()
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """``lnclone`` (from example_pcornet) with assignments + a private chain + a mapper."""
    import ovid2_pipelines

    from ovid2.tools.new_pipeline import scaffold_pipeline

    root = tmp_path / "ns" / "ovid2_pipelines"
    root.mkdir(parents=True)
    scaffold_pipeline(
        "lnclone", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=root
    )
    pkg = root / "lnclone"
    (pkg / "transforms").mkdir(exist_ok=True)
    (pkg / "transforms" / "upper_sex.yaml").write_text(
        "v: 1\nsteps:\n  - op: to_upper\n    columns: [SEX, RACE]\n", encoding="utf-8"
    )
    (pkg / "transforms" / "assignments.yaml").write_text(
        "v: 1\n"
        "tables:\n"
        '  "*":\n'
        "    - spec: clean_whitespace_and_nulls\n"
        "  demographic:\n"
        "    - spec: upper_sex\n"
        "    - steps:\n"
        "        - op: derive_column\n"
        "          column: SEX\n"
        "          expr: \"coalesce(nullif(trim(SEX), ''), RAW_SEX, 'UN')\"\n"
        "        - op: filter_expr\n"
        '          expr: "BIRTH_DATE IS NOT NULL"\n'
        "    - spec: does_not_exist\n",
        encoding="utf-8",
    )
    (pkg / "mappers").mkdir(exist_ok=True)
    (pkg / "mappers" / "demographic.yaml").write_text(
        "v: 1\ntable: demographic\nops:\n"
        "  - op: translate_by_dict\n    column: SEX\n    dict: demographic_sex_dict\n"
        "  - op: hash_id\n    column: PATID\n"
        "  - op: apply_transforms\n    steps:\n      - op: trim\n        columns: [HISPANIC]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(root)])
    yield "lnclone"
    for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.lnclone")]:
        del sys.modules[mod]


def _steps_for(tl: lineage.TableLineage, column: str) -> list[lineage.LineageStep]:
    field = next(f for f in tl.fields if f.name == column)
    by_id = {s.id: s for s in tl.steps}
    return [by_id[i] for i in field.steps]


# --------------------------------------------------------------------- roles


def test_resolve_roles_grammar() -> None:
    role = {"scope": "field", "reads": ["order_by[].col", "partition_by"], "writes": ["out"]}
    got = resolve_roles(role, {"order_by": [{"col": "A"}], "partition_by": ["B"], "out": "C"})
    assert (got.reads, got.writes) == (["A", "B"], ["C"])
    fix = {"scope": "field", "reads": ["src2|PATID"], "inplace": ["src1:csv"]}
    got = resolve_roles(fix, {"src1": "X_DATE, Y_DATE", "src2": "NotUsed"}, literals=True)
    assert got.writes == ["X_DATE", "Y_DATE"]
    assert got.reads == ["PATID"]
    assert resolve_roles(None, {}).scope == "table"


# ------------------------------------------------------------------- service


def test_example_pcornet_demographic_chain() -> None:
    tl = lineage.table_lineage("example_pcornet", "demographic")
    assert tl.configured and tl.editable
    assert [s.stage for s in _steps_for(tl, "PATID")] == ["format", "deduplicate"]
    race_eth = _steps_for(tl, "RACE_ETH_WHITE")
    assert [s.op for s in race_eth] == ["constant", "race_eth"]
    assert race_eth[-1].source_doc == "fixers"
    assert race_eth[-1].step_path == "fixer_chains.demographic[0]"
    sex = next(f for f in tl.fields if f.name == "SEX")
    assert sex.valueset and "M" in sex.valueset
    assert sex.sources == ["sex"]
    zip_code = next(f for f in tl.fields if f.name == "ZIP_CODE")
    assert zip_code.status == "extra"  # projected, but not a PCORnet 7.0 column
    race_me = next(f for f in tl.fields if f.name == "RACE_ETH_ME_NA")
    assert race_me.status == "constant"  # '' and race_eth does not write it
    white = next(f for f in tl.fields if f.name == "RACE_ETH_WHITE")
    assert white.status == "ok"  # '' at format, derived by race_eth
    dedup = next(s for s in tl.steps if s.stage == "deduplicate")
    assert dedup.id in tl.table_steps and dedup.reads == ["PATID"]


@needs_acme
def test_semantic_join_formatter_and_mapper_yaml() -> None:
    tl = lineage.table_lineage("acme_from_disc", "condition")
    joins = [s for s in tl.steps if s.op == "join"]
    assert joins and all(s.id in tl.table_steps for s in joins)
    status = _steps_for(tl, "CONDITION_STATUS")
    assert [s.op for s in status] == ["project", "case_map", "translate_by_dict"]
    assert status[-1].source_ref == {"table": "condition"}
    assert status[-1].step_path == "ops[1]"
    report = next(f for f in tl.fields if f.name == "REPORT_DATE")
    assert report.sources == ["visit_visit_start_date"]


@needs_legacy
def test_legacy_port_is_read_only_and_derived() -> None:
    tl = lineage.table_lineage("site", "lab_result_cm")
    assert not tl.editable
    assert all(not s.editable for s in tl.steps)
    fmt = next(s for s in tl.steps if s.stage == "format")
    assert fmt.opaque and fmt.source_ref["path"].startswith("formatters_custom/")
    loinc = _steps_for(tl, "LAB_LOINC")
    assert {s.stage for s in loinc} >= {"format", "map", "fix"}
    fixes = [s for s in tl.steps if s.stage == "fix"]
    assert fixes[0].op == "patid_orphans_fix" and fixes[0].tables == ["demographic"]
    assert fixes[0].source_doc == "vendor_file"
    assert tl.stage_summary["map"]["source"] == "cdm_default"
    assert tl.stage_summary["fix"]["source"] == "legacy_vendor"


def test_clone_transforms_mapper_and_udf(clone: str) -> None:
    tl = lineage.table_lineage(clone, "demographic")
    sex = _steps_for(tl, "SEX")
    ops = [(s.stage, s.op) for s in sex]
    assert ("transform", "normalize_whitespace") not in ops  # placeholder columns only
    assert ("transform", "to_upper") in ops
    derive = next(s for s in sex if s.op == "derive_column")
    assert derive.writes == ["SEX"] and "RAW_SEX" in derive.reads
    assert derive.source_doc == "assignments"
    assert derive.step_path == "tables.demographic[1].steps[0]"
    upper = next(s for s in sex if s.op == "to_upper")
    assert upper.source_ref == {"name": "upper_sex", "scope": "pipeline"} and upper.editable
    assert [s.op for s in sex if s.stage == "map"] == ["translate_by_dict"]
    filt = next(s for s in tl.steps if s.op == "filter_expr")
    assert filt.scope == "rows" and filt.id in tl.table_steps and filt.reads == ["BIRTH_DATE"]
    missing = next(s for s in tl.steps if s.op == "missing_chain")
    assert missing.chain == "does_not_exist"
    hisp = _steps_for(tl, "HISPANIC")
    nested = next(s for s in hisp if s.op == "trim")
    assert nested.stage == "map" and nested.id.startswith("map.2.")
    raw_sex = next(f for f in tl.fields if f.name == "RAW_SEX")
    assert derive.id in raw_sex.steps
    shipped = [s for s in tl.steps if s.chain == "clean_whitespace_and_nulls"]
    assert shipped and not any(s.editable for s in shipped)
    assert "__REPLACE_WITH_YOUR_COLUMNS__" in tl.extra_columns
    summary = lineage.package_lineage(clone)
    demo = next(t for t in summary["tables"] if t["table"] == "demographic")
    assert demo["has_udf"] and "upper_sex" in demo["chains"]


def test_unknown_table_and_package() -> None:
    with pytest.raises(LookupError):
        lineage.table_lineage("example_pcornet", "not_a_table")
    with pytest.raises(LookupError):
        lineage.package_lineage("no_such_pipeline")


def test_navigator_tree_groupings() -> None:
    tree = lineage.navigator_tree("cdm")
    labels = {g["label"]: g for g in tree["groups"]}
    assert "pcornet_7_0" in labels
    names = {p["pipeline"] for p in labels["pcornet_7_0"]["pipelines"]}
    assert "example_pcornet" in names
    by_template = {
        p["pipeline"]: p["template"]
        for g in lineage.navigator_tree("template")["groups"]
        for p in g["pipelines"]
    }
    assert by_template["example_pcornet"] == "pcornet_etl"
    profile = lineage.navigator_tree("cdm_profile")
    assert any(g["key"] == ["pcornet_7_0", "default"] for g in profile["groups"])
    if HAS_LEGACY:
        assert "site" in names
        assert by_template["site"] == "legacy_port"
        assert any(g["key"] == ["pcornet_7_0", "legacy_ovid"] for g in profile["groups"])
    with pytest.raises(ValueError):
        lineage.navigator_tree("bogus")


@needs_legacy
def test_read_source_guards() -> None:
    doc = lineage.read_source("site", "partners/site/fixer_scripts/demographic_fixer.py")
    assert "race_eth_fix" in doc["text"] and doc["readonly"]
    cdm = lineage.read_source("site", "cdm:pcornet_7_0/mappers/demographic.yaml")
    assert "legacy_select" in cdm["text"]
    for bad in (
        "partners/site/../../../../etc/passwd",
        "partners/zeta/fixer_scripts/demographic_fixer.py",
        "cdm:omop_5_3/mappers/person.yaml",
        "cdm:pcornet_7_0/tables/demographic.yaml",
    ):
        with pytest.raises(LookupError):
            lineage.read_source("site", bad)
    with pytest.raises(LookupError):
        lineage.read_source("example_pcornet", "partners/site/fixer_scripts/demographic_fixer.py")


# ----------------------------------------------------------------------- API


def test_api_navigator_and_lineage(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/navigator?group_by=profile")
    assert r.status_code == 200, r.text
    want = {"default", "legacy_ovid"} if HAS_LEGACY else {"default"}
    assert {g["label"] for g in r.json()["groups"]} >= want
    assert web_client.get("/api/v1/navigator?group_by=nope").status_code == 422

    r = web_client.get("/api/v1/pipeline-packages/example_pcornet/lineage")
    assert r.status_code == 200, r.text
    tables = {t["table"]: t for t in r.json()["tables"]}
    assert tables["demographic"]["ops"]["fix"] == ["race_eth"]

    r = web_client.get("/api/v1/pipeline-packages/example_pcornet/lineage?table=demographic")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {f["name"] for f in body["fields"]} >= {"PATID", "SEX"}
    assert body["stage_summary"]["fix"]["count"] == 1

    assert (
        web_client.get("/api/v1/pipeline-packages/example_pcornet/lineage?table=x").status_code
        == 404
    )
    assert web_client.get("/api/v1/pipeline-packages/nope/lineage").status_code == 404
    if not HAS_LEGACY:
        return
    r = web_client.get(
        "/api/v1/pipeline-packages/site/lineage/source",
        params={"path": "partners/site/fixer_scripts/demographic_fixer.py"},
    )
    assert r.status_code == 200 and r.json()["readonly"] is True
    r = web_client.get(
        "/api/v1/pipeline-packages/site/lineage/source", params={"path": "../../etc/passwd"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------- UDFs


@pytest.fixture()
def udf_clone(clone: str) -> str:
    """The clone with only valid assignments (no missing chain)."""
    path = lineage._Context(clone).pkg / "transforms" / "assignments.yaml"
    path.write_text(path.read_text().replace("    - spec: does_not_exist\n", ""))
    return clone


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "transform_library"
    monkeypatch.setenv("OVID2_TRANSFORMS_DIR", str(path))
    return path


def test_expression_check_endpoint(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/api/v1/expressions:check",
        json={
            "expr": "coalesce(nullif(trim(SEX), ''), NOPE)",
            "pipeline": "example_pcornet",
            "table": "demographic",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["unknown_columns"] == ["NOPE"]
    bad = web_client.post("/api/v1/expressions:check", json={"expr": "reflect('a','b')"}).json()
    assert not bad["ok"] and "allow-list" in bad["errors"][0]
    fns = web_client.get("/api/v1/expressions/functions").json()
    assert "coalesce" in fns and "reflect" not in fns


def test_apply_udf_inline_private_and_library(  # type: ignore[no-untyped-def]
    web_client, udf_clone: str, library: Path
) -> None:
    clone = udf_clone
    url = f"/api/v1/pipeline-packages/{clone}/udfs:apply"
    derive = [{"op": "derive_column", "column": "SEX", "expr": "upper(SEX)"}]
    r = web_client.post(url, json={"table": "demographic", "save_to": "inline", "steps": derive})
    assert r.status_code == 200, r.text
    tl = lineage.table_lineage(clone, "demographic")
    assert sum(1 for s in tl.steps if s.op == "derive_column") == 2

    r = web_client.post(
        url,
        json={"table": "demographic", "save_to": "pipeline", "name": "sex_upper", "steps": derive},
    )
    assert r.status_code == 200, r.text
    r = web_client.post(
        url,
        json={
            "table": "encounter",
            "scope": "all_tables",
            "save_to": "library",
            "name": "lib_trim",
            "steps": [{"op": "trim", "columns": ["PATID"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert (library / "lib_trim.yaml").is_file()
    tl = lineage.table_lineage(clone, "encounter")
    assert any(s.chain == "lib_trim" and s.writes == ["PATID"] for s in tl.steps)
    usage = web_client.get("/api/v1/transform-library/lib_trim/usage").json()
    assert usage == [{"pipeline": clone, "table": "*", "fields": ["PATID"]}]

    # existing chain, idempotent
    for _ in range(2):
        r = web_client.post(url, json={"table": "vital", "chain": "lib_trim"})
        assert r.status_code == 200, r.text
    from ovid2.config.transform_assignments import load_assignments

    assert [a.spec for a in load_assignments(clone)["vital"]] == ["lib_trim"]


def test_apply_udf_rejections(web_client, udf_clone: str) -> None:  # type: ignore[no-untyped-def]
    clone = udf_clone
    url = f"/api/v1/pipeline-packages/{clone}/udfs:apply"
    evil = [{"op": "derive_column", "column": "SEX", "expr": "reflect('java.lang.Runtime','x')"}]
    before = (lineage._Context(clone).pkg / "transforms" / "assignments.yaml").read_text()
    for body in (
        {"table": "demographic", "steps": evil},
        {"table": "demographic", "steps": [{"op": "to_upper", "columns": "SEX"}]},
        {"table": "not_a_table", "steps": [{"op": "trim", "columns": ["SEX"]}]},
        {
            "table": "demographic",
            "save_to": "pipeline",
            "steps": [{"op": "trim", "columns": ["SEX"]}],
        },
        {"table": "demographic", "chain": "no_such_chain"},
        {"table": "demographic"},
    ):
        r = web_client.post(url, json=body)
        assert r.status_code == 400, (body, r.text)
    after = (lineage._Context(clone).pkg / "transforms" / "assignments.yaml").read_text()
    assert before == after
    if not HAS_LEGACY:
        return
    r = web_client.post(
        "/api/v1/pipeline-packages/site/udfs:apply",
        json={"table": "demographic", "steps": [{"op": "trim", "columns": ["SEX"]}]},
    )
    assert r.status_code == 400 and "legacy_ovid" in r.json()["detail"]
