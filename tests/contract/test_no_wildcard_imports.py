"""Contract: no wildcard imports in ``src/ovid2/``.

``from x import *`` hides dependency graphs and defeats static analysis.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.mark.contract
def test_no_wildcard_imports(src_root: Path) -> None:
    """Wildcard imports are forbidden in source modules."""
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        offenders.append(f"{py.relative_to(src_root)}:{node.lineno}")

    assert not offenders, "Wildcard imports are forbidden (AGENTS.md §Style rules):\n" + "\n".join(
        offenders
    )
