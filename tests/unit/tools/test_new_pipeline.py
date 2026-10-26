"""Pipeline template scaffolder unit tests (pipeline-not-pipeline Phase E).

Covers :mod:`ovid2.tools.new_pipeline` (copy + pipeline_settings
rewrite + guards) and :mod:`ovid2.tools.templates` (catalogue).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.tools import new_pipeline
from ovid2.tools.new_pipeline import (
    NewPipelineError,
    _rewrite_pipeline_settings,
    scaffold_pipeline,
)
from ovid2.tools.templates import get_template, list_templates

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalogue_has_three_etl_templates() -> None:
    ids = {t.id for t in list_templates(include_unavailable=True)}
    assert {"pcornet_etl", "omop_source_etl", "pcornet_to_omop"} <= ids
    for t in list_templates(include_unavailable=True):
        assert t.kind == "etl"


@pytest.mark.unit
def test_get_template_unknown_returns_none() -> None:
    assert get_template("does-not-exist") is None


@pytest.mark.unit
def test_templates_with_missing_source_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A template whose source package is absent is not offered."""
    from ovid2.tools import templates as tmod

    ghost = tmod.PipelineTemplate(
        id="ghost",
        source_pipeline="no_such_pipeline_pkg",
        cdm="pcornet_7_0",
        kind="etl",
        label="Ghost",
        description="",
    )
    monkeypatch.setitem(tmod.TEMPLATES, "ghost", ghost)
    assert "ghost" not in {t.id for t in list_templates()}
    assert "ghost" in {t.id for t in list_templates(include_unavailable=True)}
    assert get_template("ghost") is None


@pytest.mark.unit
def test_template_source_pipelines_are_installed() -> None:
    """Every template's source pipeline must be a real package."""
    import importlib

    for t in list_templates():
        mod = importlib.import_module(f"ovid2_pipelines.{t.source_pipeline}")
        assert getattr(mod, "__path__", None)


# ---------------------------------------------------------------------------
# pipeline_settings rewrite
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rewrite_replaces_identity_constants() -> None:
    original = (
        "mappers_type = 'pcornet_mappers'\n"
        "DEFAULT_PIPELINE_DB_NAME =  'CHAR_SRC'\n"
        "DATAMARTID        = 'NET01SRC'\n"
        "DATAMART_NAME     = 'Source Hospital'\n"
        "DATAVANT_KEY = 'source_hospital'\n"
        "MERGE_DATABASE_NAME = 'CHAR_SRC'\n"
        "NETWORKID = 'NET01'\n"
    )
    out = _rewrite_pipeline_settings(original, "myhospital", "pcornet_7_0")
    assert "CHAR_MYHOSPITAL" in out
    assert "NET01MYHOSPITAL" in out
    assert "DATAMART_NAME     = 'myhospital'" in out
    assert "DATAVANT_KEY = 'myhospital'" in out
    assert "NETWORKID = 'NET01'" in out
    assert "CHAR_SRC" not in out
    assert "source_hospital" not in out


@pytest.mark.unit
def test_rewrite_updates_cdm_binding() -> None:
    original = 'cdm = "omop_5_3"\nDEFAULT_UPLOAD_TYPE = "no_merge"\n'
    out = _rewrite_pipeline_settings(original, "myomop", "omop_5_3")
    assert 'cdm = "omop_5_3"' in out


# ---------------------------------------------------------------------------
# scaffold_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scaffold_copies_tree_and_rewrites(tmp_path: Path) -> None:
    dest = scaffold_pipeline(
        "myhospital",
        source_pipeline="example_pcornet",
        cdm="pcornet_7_0",
        pipelines_root=tmp_path,
    )
    assert dest == tmp_path / "myhospital"
    assert (dest / "pipeline_settings.py").is_file()
    assert (dest / "formatters").is_dir()
    assert (dest / "dictionaries.py").is_file()
    assert not (dest / "__pycache__").exists()
    settings = (dest / "pipeline_settings.py").read_text(encoding="utf-8")
    assert "CHAR_MYHOSPITAL" in settings
    assert "CHAR_EXAMPLE" not in settings


@pytest.mark.unit
def test_scaffold_refuses_existing(tmp_path: Path) -> None:
    scaffold_pipeline(
        "dup", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=tmp_path
    )
    with pytest.raises(NewPipelineError, match="already exists"):
        scaffold_pipeline(
            "dup", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=tmp_path
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["bad name", "with/slash", "", "a b!"])
def test_scaffold_rejects_bad_name(tmp_path: Path, bad: str) -> None:
    with pytest.raises(NewPipelineError, match="Invalid pipeline name"):
        scaffold_pipeline(
            bad, source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=tmp_path
        )


@pytest.mark.unit
def test_scaffold_rejects_same_name_as_source(tmp_path: Path) -> None:
    with pytest.raises(NewPipelineError, match="must differ"):
        scaffold_pipeline(
            "example_pcornet",
            source_pipeline="example_pcornet",
            cdm="pcornet_7_0",
            pipelines_root=tmp_path,
        )


@pytest.mark.unit
def test_scaffold_unknown_source(tmp_path: Path) -> None:
    with pytest.raises(NewPipelineError, match="not installed"):
        scaffold_pipeline("x", source_pipeline="nope", cdm="pcornet_7_0", pipelines_root=tmp_path)


@pytest.mark.unit
def test_scaffold_omop_template_rewrites_cdm(tmp_path: Path) -> None:
    dest = scaffold_pipeline(
        "myomop",
        source_pipeline="pcornet_to_omop",
        cdm="omop_5_3",
        pipelines_root=tmp_path,
    )
    settings = (dest / "pipeline_settings.py").read_text(encoding="utf-8")
    assert 'cdm = "omop_5_3"' in settings


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_main_requires_template_or_source(capsys) -> None:  # type: ignore[no-untyped-def]
    import argparse

    args = argparse.Namespace(name="x", template=None, source=None, cdm=None)
    rc = new_pipeline.main(args)
    assert rc == 2
    assert "template" in capsys.readouterr().err


@pytest.mark.unit
def test_list_templates_without_pipelines_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``ovid2_pipelines`` on sys.path → templates hidden, no crash.

    Regression: ``find_spec('ovid2_pipelines.x')`` raises when the parent
    namespace is absent, which used to break ``ovid2 tools`` CLI parser
    construction entirely.
    """
    import importlib.util

    from ovid2.tools import templates as tmod

    def _boom(name: str, *a: object, **k: object) -> None:
        raise ModuleNotFoundError("No module named 'ovid2_pipelines'")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert tmod.list_templates() == []
    assert tmod.get_template("pcornet_etl") is None
