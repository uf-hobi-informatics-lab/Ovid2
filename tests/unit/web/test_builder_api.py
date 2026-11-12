"""Unit tests — pipeline builder API (inspection, transform library, specs).

Exercises the JSON endpoints behind the create-pipeline wizard and the
pipeline builder through a live ``TestClient``. Package writes target a
throwaway clone of ``example_pcornet`` on an extra ``ovid2_pipelines``
namespace path, so the real ``pipelines/`` tree is never touched.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Scaffold ``bldclone`` (from example_pcornet) on an extra namespace path."""
    import ovid2_pipelines

    from ovid2.tools.new_pipeline import scaffold_pipeline

    root = tmp_path / "ns" / "ovid2_pipelines"
    root.mkdir(parents=True)
    scaffold_pipeline(
        "bldclone", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=root
    )
    monkeypatch.setattr(ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(root)])
    yield "bldclone"
    for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.bldclone")]:
        del sys.modules[mod]


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "transform_library"
    monkeypatch.setenv("OVID2_TRANSFORMS_DIR", str(path))
    return path


_UDF = """v: 1
description: Normalise SEX to upper-case, unknown when blank.
steps:
  - op: derive_column
    column: SEX
    expr: "coalesce(nullif(upper(trim(SEX)), ''), 'UN')"
"""


def test_inspect_template_lists_tables_and_identity(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/pipeline-templates/pcornet_etl?name=mysite&engine=duckdb")
    assert r.status_code == 200, r.text
    body = r.json()
    names = {t["name"]: t for t in body["tables"]}
    assert names["demographic"]["configured"] is True
    assert names["demographic"]["inputs"] == ["*Demographic_*.txt"]
    assert "race_eth" in names["demographic"]["fixers"]
    identity = {i["key"]: i for i in body["identity"]}
    assert identity["DATAMARTID"]["default"] == "NET01MYSITE"
    assert identity["DATAMARTID"]["current"] == "NET01EXAMPLE"
    ids = {b["id"]: b for b in body["bindings"]}
    assert ids["cdm"]["status"] == "ok"
    assert ids["engine.tables"]["status"] == "warn"  # geocode formatter is Spark-only
    assert body["summary"]["configured"] == sum(1 for t in body["tables"] if t["configured"])


def test_inspect_unknown_template_404(web_client) -> None:  # type: ignore[no-untyped-def]
    assert web_client.get("/api/v1/pipeline-templates/nope").status_code == 404


def test_library_crud_and_validation(web_client, library: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.put("/api/v1/transform-library/normalise_sex", json={"raw_text": _UDF})
    assert r.status_code == 200, r.text
    assert (library / "normalise_sex.yaml").is_file()
    listed = {d["name"]: d for d in web_client.get("/api/v1/transform-library").json()}
    assert listed["normalise_sex"]["scope"] == "user"
    assert listed["clean_whitespace_and_nulls"]["scope"] == "shipped"

    bad = _UDF.replace("coalesce(nullif(upper(trim(SEX)), ''), 'UN')", "reflect('a','b')")
    r = web_client.put("/api/v1/transform-library/evil", json={"raw_text": bad})
    assert r.status_code == 400
    assert "allow-list" in r.json()["detail"]
    r = web_client.put(
        "/api/v1/transform-library/badparams",
        json={"raw_text": "v: 1\nsteps:\n  - op: to_upper\n    columns: SEX\n"},
    )
    assert r.status_code == 400 and "to_upper" in r.json()["detail"]
    assert (
        web_client.put("/api/v1/transform-library/Bad-Name", json={"raw_text": _UDF}).status_code
        == 400
    )

    # Delete is human-approval gated.
    r = web_client.delete("/api/v1/transform-library/normalise_sex")
    assert r.status_code == 202
    assert r.json()["detail"]["status"] == "awaiting_approval"


def test_validate_endpoint_checks_params_and_expressions(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/api/v1/transforms:validate",
        json={
            "spec": {
                "v": 1,
                "steps": [{"op": "derive_column", "column": "X", "expr": "(select 1)"}],
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_assign_and_assignments_round_trip(web_client, clone: str, library: Path) -> None:  # type: ignore[no-untyped-def]
    assert (
        web_client.put(
            "/api/v1/transform-library/normalise_sex", json={"raw_text": _UDF}
        ).status_code
        == 200
    )
    r = web_client.post(
        "/api/v1/transform-library/normalise_sex/assign",
        json={"pipeline": clone, "tables": ["demographic"]},
    )
    assert r.status_code == 200, r.text
    got = web_client.get(f"/api/v1/pipeline-packages/{clone}/transform-assignments").json()
    assert got["exists"] and got["parsed"]["tables"]["demographic"] == [{"spec": "normalise_sex"}]
    inspect = web_client.get(f"/api/v1/pipeline-packages/{clone}/inspect").json()
    demo = next(t for t in inspect["tables"] if t["name"] == "demographic")
    assert demo["transforms"] == ["normalise_sex"]

    bad = "v: 1\ntables:\n  not_a_table:\n    - spec: normalise_sex\n"
    r = web_client.put(
        f"/api/v1/pipeline-packages/{clone}/transform-assignments", json={"raw_text": bad}
    )
    assert r.status_code == 400 and "not a table" in r.json()["detail"]
    bad = "v: 1\ntables:\n  demographic:\n    - spec: missing_chain\n"
    r = web_client.put(
        f"/api/v1/pipeline-packages/{clone}/transform-assignments", json={"raw_text": bad}
    )
    assert r.status_code == 400 and "unknown chain" in r.json()["detail"]


def test_pipeline_private_transform(web_client, clone: str) -> None:  # type: ignore[no-untyped-def]
    r = web_client.put(
        f"/api/v1/pipeline-packages/{clone}/transforms/local_fix", json={"raw_text": _UDF}
    )
    assert r.status_code == 200, r.text
    listed = web_client.get(f"/api/v1/pipeline-packages/{clone}/transforms").json()
    assert [d["name"] for d in listed] == ["local_fix"]
    assert (
        web_client.delete(f"/api/v1/pipeline-packages/{clone}/transforms/local_fix").status_code
        == 200
    )


def test_fixers_yaml_editing(web_client, clone: str) -> None:  # type: ignore[no-untyped-def]
    got = web_client.get(f"/api/v1/pipeline-packages/{clone}/fixers").json()
    assert got["source"] == "python"
    assert "race_eth" in got["raw_text"]
    text = got["raw_text"].replace(
        "{fix: race_eth, src1: RACE, src2: HISPANIC}",
        "{fix: race_eth, race_column: RACE, hispanic_column: HISPANIC}",
    )
    r = web_client.put(f"/api/v1/pipeline-packages/{clone}/fixers", json={"raw_text": text})
    assert r.status_code == 200, r.text
    assert web_client.get(f"/api/v1/pipeline-packages/{clone}/fixers").json()["source"] == "yaml"
    plan = web_client.get(f"/api/v1/pipeline-packages/{clone}/plan").json()
    assert plan["fixers"]["demographic"][0]["fix"] == "race_eth"
    r = web_client.put(
        f"/api/v1/pipeline-packages/{clone}/fixers",
        json={"raw_text": "v: 1\nfixer_chains:\n  demographic:\n    - {fix: nope}\n"},
    )
    assert r.status_code == 400 and "unknown fix" in r.json()["detail"]


def test_settings_literal_edit(web_client, clone: str) -> None:  # type: ignore[no-untyped-def]
    got = web_client.get(f"/api/v1/pipeline-packages/{clone}/settings").json()
    rows = {c["key"]: c for c in got["constants"]}
    assert rows["DATAMARTID"]["editable"] is True
    r = web_client.patch(
        f"/api/v1/pipeline-packages/{clone}/settings",
        json={"values": {"DATAMARTID": "NET01BLD", "DEFAULT_UPLOAD_TYPE": "merge"}},
    )
    assert r.status_code == 200, r.text
    rows = {
        c["key"]: c
        for c in web_client.get(f"/api/v1/pipeline-packages/{clone}/settings").json()["constants"]
    }
    assert rows["DATAMARTID"]["value"] == "NET01BLD"
    r = web_client.patch(
        f"/api/v1/pipeline-packages/{clone}/settings",
        json={"values": {"DEFAULT_UPLOAD_TYPE": "sideways"}},
    )
    assert r.status_code == 400
    r = web_client.patch(
        f"/api/v1/pipeline-packages/{clone}/settings", json={"values": {"NOPE": 1}}
    )
    assert r.status_code == 400


def test_bindings_endpoint(web_client, clone: str) -> None:  # type: ignore[no-untyped-def]
    rows = web_client.get(f"/api/v1/pipeline-packages/{clone}/bindings?engine=pyspark").json()
    assert any(b["id"] == "cdm" and b["status"] == "ok" for b in rows)


def test_fixer_catalogue(web_client) -> None:  # type: ignore[no-untyped-def]
    rows = web_client.get("/api/v1/fixers?cdm=omop_5_3").json()
    names = {r["name"] for r in rows}
    assert "patid_orphans" in names and "race_eth" not in names
