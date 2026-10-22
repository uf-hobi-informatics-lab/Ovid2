"""Unit tests for the formatter spec loader and validator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

from ovid2.formatters.spec import (
    FormatterSpec,
    SpecNotFoundError,
    SpecValidationError,
    find_and_load,
    load_from_dict,
    load_from_yaml,
    spec,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pipelines_root"


@pytest.fixture(scope="module", autouse=True)
def _add_fixture_pipelines_to_syspath() -> None:
    """Expose ``ovid2_pipelines.testpipeline`` for the tests in this module."""
    if str(_FIXTURES) not in sys.path:
        sys.path.insert(0, str(_FIXTURES))


@pytest.mark.unit
def test_spec_factory_injects_version() -> None:
    """The ``spec(...)`` helper injects ``v: 1``."""
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"x": {"glob": "*"}},
        projections={"PATID": "patid"},
    )
    assert raw["v"] == 1
    assert raw["formatter"] == "demographic"


@pytest.mark.unit
def test_load_from_dict_passes_schema() -> None:
    """A well-formed dict passes validation."""
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"demographic": {"glob": "*"}},
        projections={"PATID": "patid"},
    )
    result = load_from_dict(raw, pipeline="testpipeline")
    assert isinstance(result, FormatterSpec)
    assert result.pipeline == "testpipeline"
    assert result.adapter == "pcornet_passthrough"
    assert result.sources["demographic"].glob == "*"


@pytest.mark.unit
def test_load_from_dict_rejects_unknown_adapter() -> None:
    """Unknown adapters trip the JSON schema enum."""
    raw = spec(formatter="demographic", adapter="not_a_real_adapter")
    with pytest.raises(SpecValidationError):
        load_from_dict(raw, pipeline="testpipeline")


@pytest.mark.unit
def test_load_from_dict_requires_class_for_custom_adapter() -> None:
    """The ``custom`` adapter must ship a ``class`` field."""
    raw = spec(formatter="vital", adapter="custom")
    with pytest.raises(SpecValidationError):
        load_from_dict(raw, pipeline="testpipeline")


@pytest.mark.unit
def test_projection_column_names_must_be_uppercase() -> None:
    """Projections must use uppercase CDM-style column names."""
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"x": {"glob": "*"}},
        projections={"lowercase": "expr"},
    )
    with pytest.raises(SpecValidationError):
        load_from_dict(raw, pipeline="testpipeline")


@pytest.mark.unit
def test_find_and_load_yaml_spec() -> None:
    """YAML specs on disk are discoverable via ``find_and_load``."""
    result = find_and_load(pipeline="testpipeline", table="demographic")
    assert result.adapter == "pcornet_passthrough"
    assert result.projections["PATID"] == "patid"
    assert result.source_path is not None
    assert result.source_path.name == "demographic.yaml"


@pytest.mark.unit
def test_find_and_load_python_spec() -> None:
    """Python-factory specs are preferred over YAML when both are present."""
    result = find_and_load(pipeline="testpipeline", table="encounter")
    assert result.adapter == "pcornet_passthrough"
    assert result.projections["ENCOUNTERID"] == "encounterid"
    assert result.source_path is None  # loaded from module, not file


@pytest.mark.unit
def test_find_and_load_missing_raises() -> None:
    """A missing spec raises :class:`SpecNotFoundError`."""
    with pytest.raises(SpecNotFoundError):
        find_and_load(pipeline="testpipeline", table="does_not_exist")


@pytest.mark.unit
def test_load_from_yaml_direct(tmp_path: Path) -> None:
    """Loading a YAML file directly bypasses the search path."""
    yaml_body = (
        "v: 1\n"
        "formatter: demographic\n"
        "adapter: pcornet_passthrough\n"
        "sources:\n"
        "  demographic:\n"
        "    glob: '*'\n"
        "projections:\n"
        "  PATID: 'patid'\n"
    )
    path = tmp_path / "demographic.yaml"
    path.write_text(yaml_body, encoding="utf-8")
    result = load_from_yaml(path, pipeline="testpipeline")
    assert result.formatter == "demographic"
    assert result.source_path == path
