"""Unit tests for the W4 write path in ``formatters_svc`` and ``mappers_svc``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.web.models import FormatterWrite, MapperWrite
from ovid2.web.services import formatters_svc, mappers_svc


@pytest.fixture()
def pipeline_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_xdg: Path
) -> str:
    """Create a pipeline package with empty formatters/mappers dirs on disk."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_writable"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "MAPPERS_TYPE = 'pcornet'\nDEFAULT_PIPELINE_DB_NAME = 'tp'\n",
        encoding="utf-8",
    )
    # Legacy Python-dict spec that must be immutable via the web tier.
    (pkg / "formatters" / "legacy.py").write_text(
        "SPEC = {'v': 1, 'formatter': 'legacy', 'adapter': 'custom'}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_writable"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


_VALID_FORMATTER = (
    "v: 1\n"
    "formatter: demographic\n"
    "adapter: source_passthrough\n"
    'sources:\n  demographic:\n    glob: "*Demographic*"\n'
    "projections:\n  PATID: patid\n"
)

_VALID_MAPPER = "v: 1\ntable: demographic\nops: []\n"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_save_new_formatter(pipeline_writable: str) -> None:
    """A valid YAML is written verbatim to ``<pipeline>/formatters/<table>.yaml``."""
    result = formatters_svc.save_formatter(
        pipeline_writable,
        "demographic",
        FormatterWrite(raw_text=_VALID_FORMATTER),
    )
    assert result.action == "created"
    assert result.path.endswith("/demographic.yaml")
    assert Path(result.path).read_text().startswith("v: 1")


@pytest.mark.unit
def test_save_updates_existing(pipeline_writable: str) -> None:
    """A second save overwrites the file and returns ``updated``."""
    formatters_svc.save_formatter(
        pipeline_writable, "demographic", FormatterWrite(raw_text=_VALID_FORMATTER)
    )
    updated = _VALID_FORMATTER.replace("PATID: patid", "PATID: pid")
    result = formatters_svc.save_formatter(
        pipeline_writable, "demographic", FormatterWrite(raw_text=updated)
    )
    assert result.action == "updated"
    assert "pid" in Path(result.path).read_text()


@pytest.mark.unit
def test_save_refused_for_invalid_yaml(pipeline_writable: str) -> None:
    """Malformed YAML is refused without touching disk."""
    with pytest.raises(formatters_svc.FormatterError):
        formatters_svc.save_formatter(
            pipeline_writable,
            "broken",
            FormatterWrite(raw_text="{: not: valid ["),
        )
    pipeline_path = Path(sys.modules["ovid2_pipelines.tp_writable"].__path__[0])
    assert not (pipeline_path / "formatters" / "broken.yaml").exists()


@pytest.mark.unit
def test_save_refused_for_schema_error(pipeline_writable: str) -> None:
    """Missing required ``v`` key triggers a schema violation."""
    bad = "formatter: bad\nadapter: source_passthrough\n"
    with pytest.raises(formatters_svc.FormatterError, match="Schema"):
        formatters_svc.save_formatter(
            pipeline_writable, "bad", FormatterWrite(raw_text=bad)
        )


@pytest.mark.unit
def test_save_refused_for_python_dict(pipeline_writable: str) -> None:
    """A ``legacy.py`` on disk shadows any YAML write attempt."""
    with pytest.raises(formatters_svc.FormatterError, match="Python-dict"):
        formatters_svc.save_formatter(
            pipeline_writable,
            "legacy",
            FormatterWrite(raw_text=_VALID_FORMATTER),
        )


@pytest.mark.unit
def test_delete_removes_yaml(pipeline_writable: str) -> None:
    """Delete unlinks the YAML file."""
    formatters_svc.save_formatter(
        pipeline_writable, "demographic", FormatterWrite(raw_text=_VALID_FORMATTER)
    )
    result = formatters_svc.delete_formatter(pipeline_writable, "demographic")
    assert result.action == "deleted"
    assert formatters_svc.get_formatter(pipeline_writable, "demographic") is None


@pytest.mark.unit
def test_delete_refuses_python(pipeline_writable: str) -> None:
    """Delete refuses to touch a Python-dict spec."""
    with pytest.raises(formatters_svc.FormatterError):
        formatters_svc.delete_formatter(pipeline_writable, "legacy")


@pytest.mark.unit
def test_delete_missing_raises(pipeline_writable: str) -> None:
    """Deleting an unknown table raises."""
    with pytest.raises(formatters_svc.FormatterError):
        formatters_svc.delete_formatter(pipeline_writable, "nope")


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_save_new_mapper(pipeline_writable: str) -> None:
    """A valid YAML mapper is written to ``<pipeline>/mappers/<table>.yaml``."""
    result = mappers_svc.save_mapper(
        pipeline_writable, "demographic", MapperWrite(raw_text=_VALID_MAPPER)
    )
    assert result.action == "created"
    assert result.path.endswith("/demographic.yaml")


@pytest.mark.unit
def test_save_refused_for_invalid_mapper(pipeline_writable: str) -> None:
    """Schema-invalid mapper YAML is refused."""
    bad = "v: 1\ntable: bad\nops: not-a-list\n"
    with pytest.raises(mappers_svc.MapperError):
        mappers_svc.save_mapper(
            pipeline_writable, "bad", MapperWrite(raw_text=bad)
        )


@pytest.mark.unit
def test_delete_removes_mapper(pipeline_writable: str) -> None:
    """Delete removes the YAML mapper."""
    mappers_svc.save_mapper(
        pipeline_writable, "demographic", MapperWrite(raw_text=_VALID_MAPPER)
    )
    result = mappers_svc.delete_mapper(pipeline_writable, "demographic")
    assert result.action == "deleted"
