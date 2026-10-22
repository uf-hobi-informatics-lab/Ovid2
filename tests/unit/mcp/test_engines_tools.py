"""Unit tests for the engine MCP tools (E1)."""

from __future__ import annotations

import inspect

import pytest


ENGINE_TOOL_NAMES = frozenset(
    {
        "list_engine_types",
        "list_engines",
        "get_engine",
        "create_engine",
        "update_engine",
        "delete_engine",
        "test_engine",
    }
)


@pytest.mark.unit
def test_all_seven_engine_tools_are_discovered() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    names = {s.name for s in specs}
    missing = ENGINE_TOOL_NAMES - names
    assert not missing, f"missing engine tools: {missing}"


@pytest.mark.unit
def test_engine_tools_have_expected_shape() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for name in ENGINE_TOOL_NAMES:
        spec = specs[name]
        assert spec.description.strip(), f"{name}: empty description"
        assert callable(spec.run), name
        assert inspect.iscoroutinefunction(spec.run), f"{name}: run must be async"


@pytest.mark.unit
def test_parameterless_tools_have_no_args_model() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for name in ("list_engines", "list_engine_types"):
        assert specs[name].args_model is None, name


@pytest.mark.unit
def test_parameterized_tools_have_a_name_arg() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for name in ("get_engine", "delete_engine", "test_engine"):
        model = specs[name].args_model
        assert model is not None, name
        assert "name" in model.model_fields, f"{name}: 'name' field missing"


@pytest.mark.unit
def test_write_tools_expose_the_write_fields() -> None:
    from ovid2.mcp import tools as tools_pkg

    specs = {s.name: s for s in tools_pkg.discover_tools()}
    for tool_name in ("create_engine", "update_engine"):
        model = specs[tool_name].args_model
        assert model is not None, tool_name
        fields = set(model.model_fields.keys())
        assert {"name", "type", "args", "secrets", "secret_ref"} <= fields, (
            f"{tool_name}: missing one of the write fields ({fields})"
        )
