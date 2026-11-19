"""Unit tests — deleting pipeline packages and the navigator's unused filter.

Covers :mod:`ovid2.tools.delete_pipeline`,
:mod:`ovid2.web.services.pipeline_packages_svc`, ``navigator_tree``'s
``include_unused`` filter, the gated ``DELETE /api/v1/pipeline-packages/{name}``
and the HTML ``POST /pipeline-packages/{name}/delete`` route, plus the
``ovid2 pipeline delete`` CLI. Every test works on a throwaway clone in a
temporary namespace directory, never on a shipped package.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def scratch(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A ``delclone`` package (cloned from example_pcornet) in a temp namespace dir."""
    import ovid2_pipelines

    from ovid2.tools.new_pipeline import scaffold_pipeline

    root = tmp_path / "ns" / "ovid2_pipelines"
    root.mkdir(parents=True)
    scaffold_pipeline(
        "delclone", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=root
    )
    monkeypatch.setattr(ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(root)])
    importlib.invalidate_caches()
    yield root / "delclone"
    for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.delclone")]:
        del sys.modules[mod]


def _write_config(name: str, pipeline: str) -> Path:
    cfg_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "pipelines"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "kind": "etl",
                "pipeline": pipeline,
                "cdm": "pcornet_7_0",
                "input_address": "in",
                "output_address": "out",
                "trigger": {"type": "manual"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _tree_names(**kw: object) -> set[str]:
    from ovid2.web.services import pipeline_lineage_svc

    tree = pipeline_lineage_svc.navigator_tree("cdm", **kw)  # type: ignore[arg-type]
    return {p["pipeline"] for g in tree["groups"] for p in g["pipelines"]}


# ---------------------------------------------------------------- navigator


def test_navigator_hides_unused_packages(scratch: Path) -> None:
    """A package with no run config is hidden unless include_unused; templates always show."""
    assert "delclone" not in _tree_names()
    assert "example_pcornet" in _tree_names()  # template source
    assert "delclone" in _tree_names(include_unused=True)
    _write_config("delclone_cfg", "delclone")
    assert "delclone" in _tree_names()


def test_navigator_drops_package_when_last_config_deleted(scratch: Path) -> None:
    """Deleting the last run config removes the package on the very next request."""
    from ovid2.web.services import pipelines_svc

    _write_config("delclone_cfg", "delclone")
    assert "delclone" in _tree_names()
    pipelines_svc.delete_pipeline("delclone_cfg", actor="test")
    assert "delclone" not in _tree_names()


def test_navigator_reports_unused_count(scratch: Path) -> None:
    from ovid2.web.services import pipeline_lineage_svc

    tree = pipeline_lineage_svc.navigator_tree("cdm")
    assert tree["include_unused"] is False
    assert tree["unused_count"] >= 1
    full = pipeline_lineage_svc.navigator_tree("cdm", include_unused=True)
    flags = {p["pipeline"]: p for g in full["groups"] for p in g["pipelines"]}
    assert flags["delclone"]["unused"] is True
    assert flags["example_pcornet"]["protected"] is True


# ------------------------------------------------------------------ service


def test_delete_package_moves_to_trash_and_purges_modules(scratch: Path) -> None:
    from ovid2.tools import delete_pipeline as tool
    from ovid2.web.services import pipeline_packages_svc, pipelines_meta_svc

    importlib.import_module("ovid2_pipelines.delclone")
    assert "ovid2_pipelines.delclone" in sys.modules
    _write_config("delclone_cfg", "delclone")

    with pytest.raises(pipeline_packages_svc.PackageInUse) as info:
        pipeline_packages_svc.delete_package("delclone")
    assert info.value.configs == ["delclone_cfg"]
    assert scratch.is_dir()

    res = pipeline_packages_svc.delete_package("delclone", with_configs=True, actor="test")
    assert res.deleted_configs == ["delclone_cfg"]
    assert not scratch.exists()
    trash = Path(res.trash)
    assert trash.parent == tool.trash_dir() and (trash / "pipeline_settings.py").is_file()
    assert "ovid2_pipelines.delclone" not in sys.modules
    assert "delclone" not in pipelines_meta_svc.discover_pipelines()
    assert "delclone" not in _tree_names(include_unused=True)


def test_delete_refuses_protected_unknown_and_unsafe(scratch: Path) -> None:
    from ovid2.web.services import pipeline_packages_svc as svc

    with pytest.raises(svc.PackageDeleteError, match="template"):
        svc.delete_package("example_pcornet")
    for bad in ("no_such_pkg", "../etc", "delclone/../x", ""):
        with pytest.raises(svc.PackageDeleteError):
            svc.delete_package(bad)
    assert scratch.is_dir()


def test_delete_refuses_symlinked_package(scratch: Path, tmp_path: Path) -> None:
    from ovid2.web.services import pipeline_packages_svc as svc

    link = scratch.parent / "dellink"
    link.symlink_to(scratch, target_is_directory=True)
    with pytest.raises(svc.PackageDeleteError, match="symlink"):
        svc.delete_package("dellink")
    assert scratch.is_dir()


# ---------------------------------------------------------------- web / API


def test_api_navigator_include_unused(web_client, scratch: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/navigator")
    assert r.status_code == 200
    names = {p["pipeline"] for g in r.json()["groups"] for p in g["pipelines"]}
    assert "delclone" not in names
    r = web_client.get("/api/v1/navigator?include_unused=true")
    names = {p["pipeline"] for g in r.json()["groups"] for p in g["pipelines"]}
    assert "delclone" in names
    assert "no-store" in r.headers.get("cache-control", "")


def test_api_delete_package_is_gated(web_client, scratch: Path) -> None:  # type: ignore[no-untyped-def]
    from ovid2.web.models import ApprovalDecision
    from ovid2.web.services import approvals_svc

    assert web_client.delete("/api/v1/pipeline-packages/no_such_pkg").status_code == 404
    _write_config("delclone_cfg", "delclone")
    r = web_client.delete("/api/v1/pipeline-packages/delclone")
    assert r.status_code == 409 and r.json()["detail"]["configs"] == ["delclone_cfg"]

    r = web_client.delete("/api/v1/pipeline-packages/delclone?with_configs=true")
    assert r.status_code == 202, r.text
    approval_id = r.json()["detail"]["approval_id"]
    assert scratch.is_dir()
    approvals_svc.decide(approval_id, True, ApprovalDecision(decided_by="tester"))
    r = web_client.delete("/api/v1/pipeline-packages/delclone?with_configs=true")
    assert r.status_code == 200, r.text
    assert r.json()["deleted_configs"] == ["delclone_cfg"]
    assert not scratch.exists()


def test_html_delete_post_and_hub_button(web_client, scratch: Path) -> None:  # type: ignore[no-untyped-def]
    page = web_client.get("/pipeline-packages/delclone")
    assert page.status_code == 200
    assert "/pipeline-packages/delclone/delete" in page.text
    # Template sources never offer a delete.
    assert (
        "/pipeline-packages/example_pcornet/delete"
        not in web_client.get("/pipeline-packages/example_pcornet").text
    )
    r = web_client.post(
        "/pipeline-packages/delclone/delete",
        data={"next": "/navigator"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/navigator"
    assert not scratch.exists()
    assert web_client.get("/pipeline-packages/delclone").status_code == 404


def test_html_delete_rejects_offsite_redirect(web_client, scratch: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/pipeline-packages/delclone/delete",
        data={"next": "//evil.example.org/"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/pipeline-packages"


# ---------------------------------------------------------------------- CLI


def test_cli_pipeline_delete(scratch: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ovid2.cli import __main__ as cli_main

    _write_config("delclone_cfg", "delclone")
    parser = cli_main._build_parser()
    args = parser.parse_args(["pipeline", "delete", "delclone", "--yes"])
    assert args._entry(args) == 1
    assert "--with-configs" in capsys.readouterr().out
    assert scratch.is_dir()
    args = parser.parse_args(["pipeline", "delete", "delclone", "--with-configs", "--yes"])
    assert args._entry(args) == 0
    out = capsys.readouterr().out
    assert "Moved" in out and "delclone_cfg" in out
    assert not scratch.exists()
