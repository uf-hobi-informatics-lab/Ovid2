"""Unit tests for the Ovid Data Space MCP tools."""

from __future__ import annotations

import inspect

import pytest

ODS_TOOL_NAMES = frozenset(
    {
        "ods_list_pipelines",
        "ods_list_dimensions",
        "ods_list_batches",
        "ods_list_tables",
        "ods_resolve",
    }
)


@pytest.mark.unit
def test_all_five_ods_tools_are_discovered() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    names = {s.name for s in specs}
    missing = ODS_TOOL_NAMES - names
    assert not missing, f"missing ODS tools: {missing}"


@pytest.mark.unit
def test_ods_tools_have_expected_shape() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for name in ODS_TOOL_NAMES:
        spec = specs[name]
        assert spec.description.strip(), f"{name}: empty description"
        assert callable(spec.run), name
        assert inspect.iscoroutinefunction(spec.run), f"{name}: run must be async"


@pytest.mark.unit
def test_parameterless_tools_have_no_args_model() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for name in ("ods_list_pipelines", "ods_list_dimensions"):
        assert specs[name].args_model is None, name


@pytest.mark.unit
def test_list_batches_expects_pipeline() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    model = specs["ods_list_batches"].args_model
    assert model is not None
    assert "pipeline" in model.model_fields


@pytest.mark.unit
def test_list_tables_expects_pipeline_and_batch() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    model = specs["ods_list_tables"].args_model
    assert model is not None
    assert "pipeline" in model.model_fields
    assert "batch_id" in model.model_fields


@pytest.mark.unit
def test_resolve_expects_path() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    model = specs["ods_resolve"].args_model
    assert model is not None
    assert "path" in model.model_fields
    assert "dim" in model.model_fields
