"""Contract: web-tier code must not import from pipeline packages.

The web control panel is layered on top of Ovid2 — it observes and drives
the pipeline but never imports pipeline modules. Concretely, no file
under ``src/ovid2/web/`` may import from:

- ``ovid2.core``
- ``ovid2.transformations``
- ``ovid2.formatters``
- ``ovid2.mappers``
- ``ovid2.semantic``

Approved dependencies for the web tier:

- ``ovid2.config`` (paths, address configs, settings)
- ``ovid2.installer`` (doctor)
- ``ovid2.display.reader`` (JSONL tail helper only)
- ``ovid2.scheduler.batch_tracker`` (canonical run history)
- ``ovid2.cli.pipeline`` — placeholder for W2 (pipeline listing helpers)

Rationale: this keeps the web tier install-light (no pyspark dependency),
avoids importing Spark modules on every HTTP request, and enforces the
"web is a client of the pipeline, not part of it" architecture called
out in the W1 plan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_ROOTS: tuple[str, ...] = (
    "ovid2.core",
    "ovid2.transformations",
    "ovid2.formatters",
    "ovid2.mappers",
    "ovid2.semantic",
)


def _module_target(node: ast.AST) -> str | None:
    """Return the fully-qualified module referenced by an import node."""
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


def _is_forbidden(target: str) -> bool:
    """Return True if ``target`` resolves into any forbidden root."""
    return any(target == root or target.startswith(root + ".") for root in FORBIDDEN_ROOTS)


@pytest.mark.contract
def test_web_does_not_import_pipeline(src_root: Path) -> None:
    """No web module may import pipeline packages."""
    web_root = src_root / "web"
    if not web_root.is_dir():
        pytest.skip("Web tier not present.")

    offenders: list[str] = []
    for py in web_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            target = _module_target(node)
            if target and _is_forbidden(target):
                offenders.append(f"{py.relative_to(src_root)}:{node.lineno} imports {target}")

    assert not offenders, (
        "Web-tier code must not import pipeline packages "
        "(see .opencode/knowledge/delta/adr-web-control-panel.md):\n" + "\n".join(offenders)
    )
