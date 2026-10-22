"""Contract: pipeline code must not import from ``ovid2.display``.

The display subsystem subscribes to the event bus; the pipeline emits events
and never reaches back into display. This test walks ``core/``,
``transformations/`` and ``formatters/`` and rejects any import that resolves
into ``ovid2.display``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCANNED_DIRS: tuple[str, ...] = ("core", "transformations", "formatters")
FORBIDDEN_ROOT: str = "ovid2.display"


def _module_target(node: ast.AST) -> str | None:
    """Return the fully-qualified module name referenced by an import node."""
    if isinstance(node, ast.Import):
        # ``import ovid2.display.bus`` — take the first alias.
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


@pytest.mark.contract
def test_pipeline_does_not_import_display(src_root: Path) -> None:
    """Engine code must not import ``ovid2.display`` or any submodule."""
    offenders: list[str] = []
    for subdir in SCANNED_DIRS:
        root = src_root / subdir
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                target = _module_target(node)
                if target and (target == FORBIDDEN_ROOT or target.startswith(FORBIDDEN_ROOT + ".")):
                    offenders.append(f"{py.relative_to(src_root)}:{node.lineno} imports {target}")

    assert not offenders, (
        "Pipeline code must not import from ovid2.display "
        "(AGENTS.md §Display isolation):\n" + "\n".join(offenders)
    )
