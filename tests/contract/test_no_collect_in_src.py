"""Contract: no eager Spark actions in engine code.

Enforces the lazy-Spark invariant (``AGENTS.md`` §Core design principles):
``core/``, ``transformations/``, ``transforms/``, ``mappers/``,
``formatters/`` and ``semantic/`` must never call the following methods,
which materialize a DataFrame and defeat lazy execution:

* ``.collect()``
* ``.toPandas()``
* ``.take(...)``
* ``.first()``
* ``.count()``

**Exceptions** — a line ending in ``# noqa: OVID2001`` is intentionally
allowed (e.g. deliberate reference-table warm-up in
``ovid2.core.pipeline``). Contract-test regressions must justify their
suppression in the same commit.

The scanner uses the Python AST so mentions in docstrings, string
literals, and comments do not count.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_METHODS: frozenset[str] = frozenset(
    {
        "collect",
        "toPandas",
        "take",
        "first",
        "count",
    }
)

SUPPRESSION_MARKER: str = "OVID2001"
SCANNED_DIRS: tuple[str, ...] = (
    "core",
    "transformations",
    "transforms",
    "mappers",
    "formatters",
    "semantic",
)


def _line_has_suppression(source_lines: list[str], line_no: int) -> bool:
    """Return True if the given line ends with the OVID2001 noqa marker."""
    if 1 <= line_no <= len(source_lines):
        return SUPPRESSION_MARKER in source_lines[line_no - 1]
    return False


def _scan_module(py: Path) -> list[str]:
    """Return a list of ``rel_path:line meth`` offender strings for one file."""
    text = py.read_text(encoding="utf-8")
    source_lines = text.splitlines()
    tree = ast.parse(text, filename=str(py))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        method_name = func.attr
        if method_name not in FORBIDDEN_METHODS:
            continue
        if _line_has_suppression(source_lines, node.lineno):
            continue
        offenders.append(f"{py.relative_to(py.parents[3])}:{node.lineno} .{method_name}(")
    return offenders


@pytest.mark.contract
def test_no_collect_in_engine_sources(src_root: Path) -> None:
    """No engine module may invoke an eager Spark action.

    A line-level ``# noqa: OVID2001`` suppression is honoured for the
    deliberate ``.count()`` used to warm the reference-table cache.
    """
    offenders: list[str] = []
    for subdir in SCANNED_DIRS:
        root = src_root / subdir
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            offenders.extend(_scan_module(py))

    assert not offenders, (
        "Eager Spark action found in engine code (violates lazy-Spark rule):\n"
        + "\n".join(offenders)
    )
