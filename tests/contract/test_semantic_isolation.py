"""Contract: pipeline code does not import individual vocabulary modules.

The semantic layer must be consumed through the registry API only.
This mirrors the CDM/ETL independence rule (P10): pipeline code
(``core/``, ``transformations/``, ``formatters/``, ``mappers/``) may
import :mod:`ovid2.semantic` (for :func:`get_vocabulary`, bindings, etc.)
but must never reach into :mod:`ovid2.semantic.vocabularies.<name>`.

Enforced by walking the source tree and rejecting any import whose
target starts with ``ovid2.semantic.vocabularies``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCANNED_DIRS: tuple[str, ...] = ("core", "transformations", "formatters", "mappers")
FORBIDDEN_ROOT: str = "ovid2.semantic.vocabularies"


def _module_target(node: ast.AST) -> str | None:
    """Return the fully-qualified module name referenced by an import node."""
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


@pytest.mark.contract
def test_pipeline_does_not_import_individual_vocabularies(src_root: Path) -> None:
    """Engine code must go through :mod:`ovid2.semantic` — never a specific vocabulary."""
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
                if target and (
                    target == FORBIDDEN_ROOT or target.startswith(FORBIDDEN_ROOT + ".")
                ):
                    offenders.append(
                        f"{py.relative_to(src_root)}:{node.lineno} imports {target}"
                    )

    assert not offenders, (
        "Pipeline code must not import individual vocabulary modules "
        "(use ovid2.semantic.get_vocabulary instead):\n" + "\n".join(offenders)
    )
