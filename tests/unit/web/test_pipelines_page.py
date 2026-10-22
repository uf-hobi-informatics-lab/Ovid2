"""Phase-D grouped + filterable pipelines list page."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ovid2.web.models import PipelineTrigger, PipelineWrite
from ovid2.web.services import pipelines_svc


@pytest.fixture()
def two_pipelines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_xdg: Path) -> str:
    """Register a pipeline package + two routes of different kinds."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_grp"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text("mappers_type = 'pcornet_mappers'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]

    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    for nm in ("in_g", "out_g"):
        (configs / f"{nm}.json").write_text(
            json.dumps({"src_name": "disc", "src_args": {"path": str(tmp_path / nm)}}),
            encoding="utf-8",
        )
    pipelines_svc.write_pipeline(
        "etl_one",
        PipelineWrite(
            kind="etl",
            cdm="pcornet_7_0",
            pipeline="tp_grp",
            input_address="in_g",
            output_address="out_g",
            trigger=PipelineTrigger(type="manual"),
        ),
        actor="test",
        create_only=True,
    )
    pipelines_svc.write_pipeline(
        "copy_one",
        PipelineWrite(
            kind="copy",
            pipeline="tp_grp",
            input_address="in_g",
            output_address="out_g",
            tables=["demographic"],
            trigger=PipelineTrigger(type="manual"),
        ),
        actor="test",
        create_only=True,
    )
    yield "tp_grp"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


# --------------------------------------------------------------------------- #
# grouping service
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_pipelines_grouped_groups_by_kind(two_pipelines: str) -> None:
    groups = pipelines_svc.list_pipelines_grouped()
    kinds = [g.kind for g in groups]
    assert "etl" in kinds
    assert "copy" in kinds
    # etl precedes copy (PIPELINE_KINDS order).
    assert kinds.index("etl") < kinds.index("copy")
    etl = next(g for g in groups if g.kind == "etl")
    assert [r.name for r in etl.pipelines] == ["etl_one"]


@pytest.mark.unit
def test_list_pipelines_grouped_omits_empty_kinds(two_pipelines: str) -> None:
    groups = pipelines_svc.list_pipelines_grouped()
    assert all(g.pipelines for g in groups)
    assert "analysis" not in {g.kind for g in groups}


# --------------------------------------------------------------------------- #
# page render
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pipelines_page_renders_block_per_type(web_client: object, two_pipelines: str) -> None:
    r = web_client.get("/pipelines")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.text
    assert 'data-group="etl"' in body
    assert 'data-group="copy"' in body
    # every row is server-rendered (JS-off shows them all)
    assert 'data-name="etl_one"' in body
    assert 'data-name="copy_one"' in body


@pytest.mark.unit
def test_pipelines_page_has_filter_controls(web_client: object, two_pipelines: str) -> None:
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    assert "data-pipeline-filters" in body
    assert 'data-filter="name"' in body
    assert 'data-filter="kind"' in body
    assert 'data-filter="trigger"' in body
    assert 'data-filter="enabled"' in body
    assert 'data-filter="cdm"' in body
    assert "/static/pipelines.js" in body


@pytest.mark.unit
def test_pipelines_page_uses_native_details(web_client: object, two_pipelines: str) -> None:
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    # Native <details>/<summary> collapse — no third-party JS needed.
    assert "<details" in body
    assert "<summary>" in body


@pytest.mark.unit
def test_pipelines_js_served(web_client: object, isolated_xdg: Path) -> None:
    r = web_client.get("/static/pipelines.js")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert "data-pipeline-filters" in r.text or "pipeline-row" in r.text


@pytest.mark.unit
def test_pipelines_page_empty_state(web_client: object, isolated_xdg: Path) -> None:
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    assert "No pipelines yet" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
