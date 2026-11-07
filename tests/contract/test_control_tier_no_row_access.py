"""Contract: control-plane code never reaches the row-access / data plane.

The single concrete :class:`~ovid2.drivers.base.Driver` exposes three
planes (see ``drivers/base.py`` design rule 6). This test pins the line:

1. Modules in the **control tier** — all of ``ovid2.web``, ``ovid2.mcp``,
   ``ovid2.dataspace``, ``drivers/overwrite.py``, ``drivers/probe.py`` —
   never call a row-access (``sample_rows`` / ``iter_rows`` /
   ``count_rows``) or data-plane (``read`` / ``write`` /
   ``create_complementary_tables``) driver method, and never import the
   row-access tool module (``ovid2.tools.table_ops``) or exports.
2. The ``ovid2 tools`` handlers classified as **control** in
   :data:`ovid2.cli.tools.CONTROL_TOOLS` call no row-access / data-plane
   method either.
3. Every tool name the web tier brokers is classified as control or row
   access — an unclassified brokered tool fails here.

Method names collide with unrelated callables (``fh.read()``, the web
tier's own ``dataspace_table_svc.count_rows`` broker wrapper), so module
scans flag only receivers named like a driver (``driver``, ``drv``,
``*_driver``); control-tool handler bodies flag row access on any
receiver.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest

from ovid2.drivers.base import DATA_PLANE_METHODS, ROW_ACCESS_METHODS

_CONTROL_TIER_DIRS = ("web", "mcp", "dataspace")
_CONTROL_TIER_FILES = ("drivers/overwrite.py", "drivers/probe.py")
_FORBIDDEN_IMPORTS = ("ovid2.tools.table_ops", "ovid2.exports")


def _driver_like(node: ast.expr) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    name = name.lower()
    return name in {"driver", "drv"} or name.endswith("_driver")


def _plane_violations(tree: ast.AST, *, strict_names: bool) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr not in ROW_ACCESS_METHODS | DATA_PLANE_METHODS:
            continue
        receiver = node.func.value
        # Under strict_names (whole-module scans) only driver-shaped
        # receivers count — ``dataspace_table_svc.count_rows`` is the web
        # tier's *broker wrapper*, ``fh.read()`` is a file. Handler
        # bodies (strict_names=False) flag any receiver for row access.
        if strict_names and not _driver_like(receiver):
            continue
        if not strict_names and attr in DATA_PLANE_METHODS and not _driver_like(receiver):
            continue
        out.append((node.lineno, attr))
    return out


def _control_tier_files(src_root: Path) -> list[Path]:
    files: list[Path] = []
    for d in _CONTROL_TIER_DIRS:
        files.extend(sorted((src_root / d).rglob("*.py")))
    files.extend(src_root / f for f in _CONTROL_TIER_FILES)
    return files


@pytest.mark.contract
def test_control_tier_never_calls_row_or_data_plane(src_root: Path) -> None:
    offenders: list[str] = []
    for py in _control_tier_files(src_root):
        rel = py.relative_to(src_root)
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(rel))
        for line, attr in _plane_violations(tree, strict_names=True):
            offenders.append(f"{rel}:{line} calls .{attr}()")
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.ImportFrom):
                target = node.module
            elif isinstance(node, ast.Import):
                target = node.names[0].name
            if target and any(
                target == f or target.startswith(f + ".") for f in _FORBIDDEN_IMPORTS
            ):
                offenders.append(f"{rel}:{node.lineno} imports {target}")
    assert not offenders, (
        "Control-tier code reaches the row-access / data plane — route it "
        "through the scoped executor broker instead:\n" + "\n".join(offenders)
    )


def _resolve_handler(dotted: str) -> object:
    if "." not in dotted:
        dotted = f"ovid2.cli.tools.{dotted}"
    module_path, attr = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), attr)


@pytest.mark.contract
def test_control_tools_never_touch_rows() -> None:
    from ovid2.cli.tools import CONTROL_TOOLS

    offenders: list[str] = []
    for tool, dotted in sorted(CONTROL_TOOLS.items()):
        source = textwrap.dedent(inspect.getsource(_resolve_handler(dotted)))  # type: ignore[arg-type]
        tree = ast.parse(source)
        for line, attr in _plane_violations(tree, strict_names=False):
            offenders.append(f"{tool} ({dotted}) line {line}: .{attr}()")
    assert not offenders, "Control tools call row / data-plane methods:\n" + "\n".join(offenders)


def _brokered_tool_names(src_root: Path) -> set[str]:
    """Collect the literal first token of every broker argv in the web tier."""
    names: set[str] = set()
    for py in sorted((src_root / "web").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                value = first.value
                if value in {
                    "probe-connection",
                    "browse-storage",
                    "probe-output",
                    "list-source-tables",
                    "test-engine",
                    "describe-table",
                    "sample-table",
                    "count-rows",
                    "export-table",
                } or (value.endswith(("-table", "-rows", "-storage")) and "-" in value):
                    names.add(value)
    return names


@pytest.mark.contract
def test_every_brokered_tool_is_classified(src_root: Path) -> None:
    from ovid2.cli.tools import CONTROL_TOOLS, ROW_ACCESS_TOOLS

    classified = set(CONTROL_TOOLS) | set(ROW_ACCESS_TOOLS)
    unclassified = sorted(_brokered_tool_names(src_root) - classified)
    assert not unclassified, (
        "Brokered ovid2 tools subcommands missing from CONTROL_TOOLS / "
        f"ROW_ACCESS_TOOLS in ovid2.cli.tools: {unclassified}"
    )
    assert not (set(CONTROL_TOOLS) & set(ROW_ACCESS_TOOLS))
