"""Contract: every MCP tool module declares a Pydantic ``ARGS_MODEL``.

W6 populated ``ovid2/mcp/tools/`` with one file per tool. Each file
must expose:

- ``TOOL_NAME`` — the MCP-visible tool identifier;
- ``TOOL_DESCRIPTION`` — a human-readable one-paragraph description;
- ``ARGS_MODEL`` — either ``None`` (parameter-less tool) or a subclass
  of :class:`pydantic.BaseModel`;
- ``run(client, args)`` — an ``async`` callable.

The first check below is a cheap textual grep so the invariant fails
early even if a module happens to raise at import time. The second
check imports every tool and verifies the types.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel


@pytest.mark.contract
def test_mcp_package_layout(src_root: Path) -> None:
    """The MCP package must expose a CLI subcommand and package init."""
    mcp_root = src_root / "mcp"
    assert mcp_root.is_dir(), "src/ovid2/mcp/ must exist"
    assert (mcp_root / "__init__.py").is_file(), "ovid2.mcp package needs __init__.py"
    assert (mcp_root / "cli.py").is_file(), "ovid2.mcp.cli module needs to expose register()"


@pytest.mark.contract
def test_mcp_tools_have_pydantic_models(src_root: Path) -> None:
    """Every ``mcp/tools/*.py`` module must expose ``ARGS_MODEL``."""
    tools_dir = src_root / "mcp" / "tools"
    if not tools_dir.is_dir():
        pytest.skip("MCP tools directory not present (planned for W6).")

    offenders: list[str] = []
    for py in sorted(tools_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        text = py.read_text(encoding="utf-8")
        if "ARGS_MODEL" not in text:
            offenders.append(str(py.relative_to(src_root)))

    assert not offenders, "MCP tool modules missing ARGS_MODEL:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_mcp_tools_import_cleanly(src_root: Path) -> None:
    """Every MCP tool must be importable and expose the required attributes."""
    tools_dir = src_root / "mcp" / "tools"
    if not tools_dir.is_dir():
        pytest.skip("MCP tools directory not present (planned for W6).")

    # Ensure ``src/`` is on the import path when the test runs against a
    # source checkout (installed packages need no help).
    if str(src_root.parent) not in sys.path:
        sys.path.insert(0, str(src_root.parent))

    offenders: list[str] = []
    for py in sorted(tools_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        mod_name = f"ovid2.mcp.tools.{py.stem}"
        module = importlib.import_module(mod_name)
        if not isinstance(getattr(module, "TOOL_NAME", None), str):
            offenders.append(f"{mod_name}: TOOL_NAME missing or not str")
            continue
        if not isinstance(getattr(module, "TOOL_DESCRIPTION", None), str):
            offenders.append(f"{mod_name}: TOOL_DESCRIPTION missing or not str")
            continue
        args_model = getattr(module, "ARGS_MODEL", ...)
        if args_model is ...:
            offenders.append(f"{mod_name}: ARGS_MODEL sentinel missing")
            continue
        if args_model is not None and not (
            isinstance(args_model, type) and issubclass(args_model, BaseModel)
        ):
            offenders.append(f"{mod_name}: ARGS_MODEL is not None or a BaseModel subclass")
            continue
        runner = getattr(module, "run", None)
        if runner is None or not callable(runner):
            offenders.append(f"{mod_name}: run() missing or not callable")
    assert not offenders, "MCP tool modules failed the shape check:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_mcp_tool_names_unique(src_root: Path) -> None:
    """Two MCP tool modules cannot claim the same ``TOOL_NAME``."""
    tools_dir = src_root / "mcp" / "tools"
    if not tools_dir.is_dir():
        pytest.skip("MCP tools directory not present (planned for W6).")

    names: dict[str, str] = {}
    duplicates: list[str] = []
    for py in sorted(tools_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TOOL_NAME" for t in node.targets)
            ):
                continue
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                continue
            name = node.value.value
            if name in names:
                duplicates.append(f"{py.name} reuses TOOL_NAME='{name}' (also in {names[name]})")
            else:
                names[name] = py.name
    assert not duplicates, "Duplicate MCP TOOL_NAME values:\n" + "\n".join(duplicates)
