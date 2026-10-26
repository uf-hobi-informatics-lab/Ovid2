"""Unit tests for ``ovid2.web.services.pipelines_meta_svc``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.web.services import pipelines_meta_svc


@pytest.fixture()
def in_memory_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a fake ``ovid2_pipelines.tp_web`` package under ``tmp_path``.

    The package includes a ``pipeline_settings.py`` module, a
    ``dictionaries.py`` module, an on-disk formatter YAML, and a mapper
    YAML. Removes the entry from ``sys.modules`` on teardown so
    subsequent tests do not observe it.
    """
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_web"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text('"""Test pipeline (tp_web)."""\n', encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n"
        "DEFAULT_PIPELINE_DB_NAME = 'TESTDB'\n"
        "NETWORKID = 'NET'\n",
        encoding="utf-8",
    )
    (pkg / "dictionaries.py").write_text(
        "# Small dict for a small pipeline\nSEX = {'M': 'M', 'F': 'F'}\n",
        encoding="utf-8",
    )
    (pkg / "fixer_configs.py").write_text(
        "FIXER_CHAINS = {}\n",
        encoding="utf-8",
    )
    (pkg / "formatters" / "demographic.yaml").write_text(
        "v: 1\nformatter: demographic\nadapter: source_passthrough\n"
        'sources:\n  demographic:\n    glob: "*Demographic*"\n'
        'projections:\n  PATID: "patid"\n',
        encoding="utf-8",
    )
    (pkg / "mappers" / "demographic.yaml").write_text(
        "v: 1\ntable: demographic\nops: []\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    # ovid2_pipelines is a namespace package that may have been imported
    # already — force pkgutil to re-scan by evicting it.
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_web"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


@pytest.mark.unit
def test_discover_returns_pipeline_names(in_memory_pipeline: str) -> None:
    """Discovery returns the fake pipeline alongside the fixtures/real ones."""
    names = pipelines_meta_svc.discover_pipelines()
    assert in_memory_pipeline in names


@pytest.mark.unit
def test_list_pipelines_populates_summary(in_memory_pipeline: str) -> None:
    """Summary rows carry mapper type, DB, and file counts."""
    summaries = {p.name: p for p in pipelines_meta_svc.list_pipelines()}
    assert in_memory_pipeline in summaries
    tp = summaries[in_memory_pipeline]
    assert tp.mappers_type == "pcornet_mappers"
    assert tp.default_db == "TESTDB"
    assert tp.formatter_count == 1
    assert tp.mapper_count == 1


@pytest.mark.unit
def test_get_pipeline_returns_detail(in_memory_pipeline: str) -> None:
    """Detail includes settings dict and per-file info."""
    detail = pipelines_meta_svc.get_pipeline(in_memory_pipeline)
    assert detail is not None
    assert detail.settings["mappers_type"] == "pcornet_mappers"
    assert detail.settings["NETWORKID"] == "NET"
    assert detail.pipeline_settings_file is not None
    assert detail.dictionaries_file is not None
    assert detail.fixer_configs_file is not None
    assert [f.name for f in detail.formatters] == ["demographic.yaml"]
    assert [m.name for m in detail.mappers] == ["demographic.yaml"]


@pytest.mark.unit
def test_get_pipeline_none_for_unknown(in_memory_pipeline: str) -> None:
    """Unknown pipelines surface as None."""
    assert pipelines_meta_svc.get_pipeline("no_such_pipeline_xyz") is None


@pytest.mark.unit
def test_read_pipeline_file_returns_content(in_memory_pipeline: str) -> None:
    """Whitelisted files come back with their text content."""
    result = pipelines_meta_svc.read_pipeline_file(in_memory_pipeline, "pipeline_settings.py")
    assert result is not None
    path, content = result
    assert path.name == "pipeline_settings.py"
    assert "DEFAULT_PIPELINE_DB_NAME" in content


@pytest.mark.unit
def test_read_pipeline_file_supports_formatter_paths(in_memory_pipeline: str) -> None:
    """Formatter YAMLs are reachable via ``formatters/<table>.yaml``."""
    result = pipelines_meta_svc.read_pipeline_file(
        in_memory_pipeline, "formatters/demographic.yaml"
    )
    assert result is not None
    _, content = result
    assert "adapter: source_passthrough" in content


@pytest.mark.unit
def test_read_pipeline_file_rejects_traversal(in_memory_pipeline: str) -> None:
    """A ``..`` in the relative path is rejected."""
    assert pipelines_meta_svc.read_pipeline_file(in_memory_pipeline, "../../etc/hosts") is None
    assert pipelines_meta_svc.read_pipeline_file(in_memory_pipeline, "../__init__.py") is None


@pytest.mark.unit
def test_read_pipeline_file_rejects_non_whitelisted(in_memory_pipeline: str) -> None:
    """Files outside the whitelist (e.g. ``__init__.py``) are rejected."""
    assert pipelines_meta_svc.read_pipeline_file(in_memory_pipeline, "__init__.py") is None


@pytest.mark.unit
def test_read_pipeline_file_truncates_large_files(in_memory_pipeline: str, tmp_path: Path) -> None:
    """Files larger than ``MAX_PREVIEW_BYTES`` are truncated with a marker."""
    pipelines_meta_svc.MAX_PREVIEW_BYTES = 64  # tiny cap for the test
    try:
        # Rewrite dictionaries.py with 200 bytes to trigger truncation.
        pkg = pipelines_meta_svc._pipeline_path(in_memory_pipeline)
        assert pkg is not None
        (pkg / "dictionaries.py").write_text("A" * 200, encoding="utf-8")
        result = pipelines_meta_svc.read_pipeline_file(in_memory_pipeline, "dictionaries.py")
        assert result is not None
        _, content = result
        assert "truncated" in content
    finally:
        pipelines_meta_svc.MAX_PREVIEW_BYTES = 1_048_576
