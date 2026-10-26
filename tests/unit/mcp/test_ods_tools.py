"""Unit tests for the Ovid Data Space MCP tools."""

from __future__ import annotations

import inspect

import pytest

ODS_TOOL_NAMES = frozenset({"ods_list", "ods_list_dimensions", "ods_resolve"})


def _specs() -> dict[str, object]:
    from ovid2.mcp import tools as tools_pkg

    return {s.name: s for s in tools_pkg.discover_tools()}


@pytest.mark.unit
def test_ods_tools_are_discovered() -> None:
    specs = _specs()
    missing = ODS_TOOL_NAMES - set(specs)
    assert not missing, f"missing ODS tools: {missing}"
    # The pipeline/batch-shaped tools are gone: every store is plain directories.
    assert not {"ods_list_pipelines", "ods_list_batches", "ods_list_tables"} & set(specs)


@pytest.mark.unit
def test_ods_tools_have_expected_shape() -> None:
    specs = _specs()
    for name in ODS_TOOL_NAMES:
        spec = specs[name]
        assert spec.description.strip(), f"{name}: empty description"  # type: ignore[attr-defined]
        assert inspect.iscoroutinefunction(spec.run), name  # type: ignore[attr-defined]


@pytest.mark.unit
def test_ods_list_takes_a_path() -> None:
    model = _specs()["ods_list"].args_model  # type: ignore[attr-defined]
    assert model is not None and "path" in model.model_fields
    assert model().path == "/"


@pytest.mark.unit
def test_resolve_expects_path_only() -> None:
    model = _specs()["ods_resolve"].args_model  # type: ignore[attr-defined]
    assert model is not None
    assert set(model.model_fields) == {"path"}


@pytest.mark.unit
def test_dimensions_has_no_args() -> None:
    assert _specs()["ods_list_dimensions"].args_model is None  # type: ignore[attr-defined]
