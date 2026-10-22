"""Contract: no eager Spark actions in the web tier.

Extends the ``AGENTS.md`` lazy-Spark invariant to ``src/ovid2/web/``.
Even though the web tier does not construct Spark sessions today, this
test makes sure a future author does not accidentally add
``spark.read.csv(...).collect()`` in a route handler.

Suppression: line-level ``# noqa: OVID2001`` (same marker as
``test_no_collect_in_src.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_METHODS: frozenset[str] = frozenset({"collect", "toPandas", "take", "first", "count"})
SUPPRESSION_MARKER: str = "OVID2001"


def _line_has_suppression(source_lines: list[str], line_no: int) -> bool:
    """Return True when the given line carries the ``OVID2001`` noqa marker."""
    if 1 <= line_no <= len(source_lines):
        return SUPPRESSION_MARKER in source_lines[line_no - 1]
    return False


@pytest.mark.contract
def test_no_eager_spark_in_web(src_root: Path) -> None:
    """No web module may call an eager Spark action."""
    web_root = src_root / "web"
    if not web_root.is_dir():
        pytest.skip("Web tier not present.")

    offenders: list[str] = []
    for py in web_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        source_lines = text.splitlines()
        tree = ast.parse(text, filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in FORBIDDEN_METHODS:
                continue
            if _line_has_suppression(source_lines, node.lineno):
                continue
            offenders.append(f"{py.relative_to(src_root)}:{node.lineno} .{func.attr}(")

    assert not offenders, "Eager Spark action found in web tier:\n" + "\n".join(offenders)
