"""Contract: no bare ``except:`` clauses in ``src/ovid2/``.

``AGENTS.md`` mandates that every ``except`` names its exception type. Bare
excepts swallow ``KeyboardInterrupt`` and ``SystemExit`` and hide bugs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.mark.contract
def test_no_bare_except(src_root: Path) -> None:
    """No bare ``except:`` clauses are allowed."""
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            pytest.fail(f"Failed to parse {py}: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{py.relative_to(src_root)}:{node.lineno} bare except")

    assert not offenders, (
        "Bare `except:` clauses are forbidden (AGENTS.md §Style rules):\n" + "\n".join(offenders)
    )
