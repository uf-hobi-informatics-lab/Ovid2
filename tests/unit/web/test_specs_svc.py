"""Unit tests for ``ovid2.web.services.formatters_svc`` and ``mappers_svc``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.web.services import formatters_svc, mappers_svc


@pytest.fixture()
def pipeline_with_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a pipeline with one valid + one invalid formatter and mapper."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_specs"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Valid formatter YAML.
    (pkg / "formatters" / "good.yaml").write_text(
        "v: 1\nformatter: good\nadapter: source_passthrough\n"
        'sources:\n  good:\n    glob: "*Good*"\n'
        'projections:\n  ID: "id"\n',
        encoding="utf-8",
    )
    # Invalid formatter YAML — missing required ``v`` key.
    (pkg / "formatters" / "bad.yaml").write_text(
        "formatter: bad\nadapter: source_passthrough\n",
        encoding="utf-8",
    )
    # Python-dict formatter — should be reported as ``skipped``.
    (pkg / "formatters" / "legacy.py").write_text(
        "SPEC = {'v': 1, 'formatter': 'legacy', 'adapter': 'custom'}\n",
        encoding="utf-8",
    )
    # Valid mapper YAML — mapper schema requires ``v`` and ``table``.
    (pkg / "mappers" / "good.yaml").write_text(
        "v: 1\ntable: good\nops: []\n",
        encoding="utf-8",
    )
    # Invalid mapper YAML — ``ops`` is not a list.
    (pkg / "mappers" / "bad.yaml").write_text(
        "v: 1\ntable: bad\nops: not-a-list\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_specs"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_formatters(pipeline_with_specs: str) -> None:
    """Listing returns every spec file with size + line count."""
    infos = formatters_svc.list_formatters(pipeline_with_specs)
    names = {info.name for info in infos}
    assert {"good.yaml", "bad.yaml", "legacy.py"} == names


@pytest.mark.unit
def test_get_formatter_yaml(pipeline_with_specs: str) -> None:
    """YAML source is returned with parsed structure + adapter."""
    detail = formatters_svc.get_formatter(pipeline_with_specs, "good")
    assert detail is not None
    assert detail.source_kind == "yaml"
    assert detail.adapter == "source_passthrough"
    assert detail.parsed is not None and detail.parsed["formatter"] == "good"


@pytest.mark.unit
def test_get_formatter_python(pipeline_with_specs: str) -> None:
    """Python-dict source is returned verbatim, no parsed structure."""
    detail = formatters_svc.get_formatter(pipeline_with_specs, "legacy")
    assert detail is not None
    assert detail.source_kind == "python"
    assert detail.parsed is None
    assert "SPEC" in detail.raw_text


@pytest.mark.unit
def test_get_formatter_none(pipeline_with_specs: str) -> None:
    """Unknown formatters return None."""
    assert formatters_svc.get_formatter(pipeline_with_specs, "missing") is None


@pytest.mark.unit
def test_validate_pipeline_reports_errors_and_skips(pipeline_with_specs: str) -> None:
    """Validation returns one entry per file: ok / error / skipped."""
    report = formatters_svc.validate_pipeline(pipeline_with_specs)
    by_table = {e.table: e for e in report.entries}
    assert by_table["good"].status == "ok"
    assert by_table["bad"].status == "error"
    assert by_table["legacy"].status == "skipped"


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_mappers(pipeline_with_specs: str) -> None:
    """Mapper listing returns both specs."""
    infos = mappers_svc.list_mappers(pipeline_with_specs)
    names = {info.name for info in infos}
    assert {"good.yaml", "bad.yaml"} == names


@pytest.mark.unit
def test_get_mapper_yaml(pipeline_with_specs: str) -> None:
    """Mapper YAML is parsed."""
    detail = mappers_svc.get_mapper(pipeline_with_specs, "good")
    assert detail is not None
    assert detail.source_kind == "yaml"
    assert detail.parsed is not None and detail.parsed["table"] == "good"


@pytest.mark.unit
def test_validate_mappers_flags_invalid(pipeline_with_specs: str) -> None:
    """Invalid mapper YAML surfaces as ``error``."""
    report = mappers_svc.validate_pipeline(pipeline_with_specs)
    by_table = {e.table: e for e in report.entries}
    assert by_table["good"].status == "ok"
    assert by_table["bad"].status == "error"
