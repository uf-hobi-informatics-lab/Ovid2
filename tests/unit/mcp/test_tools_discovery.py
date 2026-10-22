"""Unit tests for :mod:`ovid2.mcp.tools` discovery."""

from __future__ import annotations

import pytest
from pydantic import BaseModel


@pytest.mark.unit
def test_discover_tools_finds_every_module() -> None:
    """Every ``*.py`` under ``tools/`` (except ``__init__``) is picked up."""
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    assert len(specs) >= 30, f"expected 30+ tools, got {len(specs)}"

    # Sanity — the canonical read-oriented tools must all be present.
    names = {s.name for s in specs}
    expected = {
        "health",
        "list_addresses",
        "get_address",
        "list_pipelines",
        "get_pipeline",
        "list_runs",
        "get_run",
        "run_events",
        "run_timeline",
        "submit_run",
        "submit_move",
        "list_jobs",
        "cancel_job",
        "rerun_job",
        "install_status",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


@pytest.mark.unit
def test_discover_tools_sorted() -> None:
    """The returned tool list is name-sorted for stable client output."""
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    names = [s.name for s in specs]
    assert names == sorted(names)


@pytest.mark.unit
def test_every_tool_has_a_valid_args_model() -> None:
    """``ARGS_MODEL`` must be either ``None`` or a :class:`BaseModel` subclass."""
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    for spec in specs:
        if spec.args_model is None:
            continue
        assert issubclass(spec.args_model, BaseModel), spec.name


@pytest.mark.unit
def test_tool_descriptions_are_populated() -> None:
    """Descriptions must be non-empty so agents get useful hints."""
    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    for spec in specs:
        assert spec.description.strip(), f"{spec.name}: description is empty"


@pytest.mark.unit
def test_tool_run_is_callable() -> None:
    """Every registered runner is a callable (coroutine function)."""
    import inspect

    from ovid2.mcp import tools as tools_pkg

    specs = tools_pkg.discover_tools()
    for spec in specs:
        assert callable(spec.run), spec.name
        assert inspect.iscoroutinefunction(spec.run), f"{spec.name}: run() must be async"
