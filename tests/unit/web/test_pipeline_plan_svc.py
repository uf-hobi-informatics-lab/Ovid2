"""Unit tests for :mod:`ovid2.web.services.pipeline_plan_svc`.

Exercises the pipeline run-plan enumeration (tables + transformation
stages + per-table fixers) against the ``testpipeline`` fixture, which
ships a YAML formatter (``demographic``), a Python-factory formatter
(``encounter``), and no formatter for the rest.
"""

from __future__ import annotations

import pytest

from ovid2.web.services import pipeline_plan_svc as svc


@pytest.mark.unit
def test_transformation_stages_constant() -> None:
    assert svc.TRANSFORMATION_STAGES == (
        "format",
        "deduplicate",
        "map",
        "fix",
    )


@pytest.mark.unit
def test_get_pipeline_plan_testpipeline() -> None:
    plan = svc.get_pipeline_plan("testpipeline")
    assert plan.pipeline == "testpipeline"
    # No pipeline_settings.py → default CDM.
    assert plan.cdm == "pcornet_7_0"
    assert plan.transformations == ["format", "deduplicate", "map", "fix"]

    by_name = {t.name: t for t in plan.tables}
    # YAML formatter present.
    assert by_name["demographic"].has_formatter is True
    assert by_name["demographic"].source == "yaml"
    # Python-factory formatter present.
    assert by_name["encounter"].has_formatter is True
    assert by_name["encounter"].source == "python"
    # A CDM table with no formatter is listed but flagged.
    assert by_name["death"].has_formatter is False
    assert by_name["death"].source == ""


@pytest.mark.unit
def test_tables_match_cdm_roster_order() -> None:
    """The plan's table list mirrors the CDM's insertion order."""
    from ovid2.cdm.registry import resolve_cdm_for_pipeline

    handle = resolve_cdm_for_pipeline("testpipeline")
    _, tables = svc.list_pipeline_tables("testpipeline")
    assert [t.name for t in tables] == list(handle.tables())


@pytest.mark.unit
def test_get_pipeline_plan_unknown_pipeline_raises() -> None:
    with pytest.raises(svc.PipelinePlanError):
        svc.get_pipeline_plan("no_such_pipeline")


@pytest.mark.unit
def test_empty_pipeline_raises() -> None:
    with pytest.raises(svc.PipelinePlanError):
        svc.get_pipeline_plan("")


@pytest.mark.unit
def test_list_pipeline_fixers_no_config() -> None:
    """A pipeline with no fixer_configs.py yields empty structures."""
    per_table, multi = svc.list_pipeline_fixers("testpipeline")
    assert per_table == {}
    assert multi == []


@pytest.mark.unit
def test_formatter_source_probe_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python probe wins over YAML which wins over custom."""
    # demographic has both a YAML file and (no) python module — YAML is
    # the resolved source for it, encounter is python.
    assert svc._formatter_source("testpipeline", "encounter") == "python"
    assert svc._formatter_source("testpipeline", "demographic") == "yaml"
    assert svc._formatter_source("testpipeline", "death") == ""


@pytest.mark.unit
def test_fixer_detail_formatting() -> None:
    entry = svc._coerce_fixer_entry({"fix": "race_eth", "src1": "RACE", "src2": "HISPANIC"})
    assert entry is not None
    assert entry.fix == "race_eth"
    assert "src1=RACE" in entry.detail
    assert "src2=HISPANIC" in entry.detail


@pytest.mark.unit
def test_coerce_fixer_entry_rejects_bad_shapes() -> None:
    assert svc._coerce_fixer_entry("not a dict") is None
    assert svc._coerce_fixer_entry({}) is None
    assert svc._coerce_fixer_entry({"src1": "x"}) is None
