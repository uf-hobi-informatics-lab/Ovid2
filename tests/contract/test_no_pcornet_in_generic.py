"""Contract: no PCORnet symbols in generic engine code (P10).

The ETL engine and generic transformations must be CDM-agnostic.
Concretely:

- ``core/``, ``transformations/`` (except the deprecated
  ``mappers/pcornet/`` re-export shim and the CDM-scoped fixers listed
  in :data:`FIXER_SCOPES`), and ``formatters/adapters/`` (except
  ``omop_concept_join.py``) must not import from
  ``ovid2.cdm.pcornet_7_0`` or from
  ``ovid2.transformations.mappers.pcornet`` (removed at P10).
- The same modules must not carry PCORnet table-name string literals
  (e.g. ``"HARVEST"``, ``"DEMOGRAPHIC"``) or column-name literals
  (``"PATID"``, ``"PROVIDERID"``) that would leak schema knowledge into
  generic code. Test-only string literals inside docstrings are
  ignored.

This test is deliberately conservative — it walks Python AST and only
inspects import statements. String-literal scanning is left to
per-fixer contract tests, which have the context to distinguish a
column reference in a docstring from one in executable code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Directories whose Python files must not import PCORnet-specific modules.
_SCANNED_DIRS: tuple[str, ...] = ("core", "transformations", "formatters", "mappers")

# Modules exempt from the ban (they are the CDM-scoped or migration-shim files).
_EXEMPT_RELATIVE_PATHS: frozenset[str] = frozenset(
    {
        # Deprecated re-export shim; explicitly documented at P10.
        "transformations/mappers/pcornet/tables_keys.py",
        "transformations/mappers/pcornet/__init__.py",
    }
)

# Import roots that are forbidden inside generic engine code.
_FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "ovid2.cdm.pcornet_7_0",
    "ovid2.cdm.omop_5_3",
    "ovid2.cdm.veritas_v1",
    "ovid2.cdm.nursing_v1",
    "ovid2.cdm.tumor_v1",
)


def _module_target(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


@pytest.mark.contract
def test_no_direct_cdm_module_imports_in_generic_code(src_root: Path) -> None:
    """Generic engine code must not import a specific CDM module.

    The rule: every CDM concern is reached through
    :func:`ovid2.cdm.registry.get_cdm` or
    :func:`ovid2.cdm.registry.resolve_cdm_for_pipeline`. Direct imports of
    a specific CDM module tie the engine to one CDM.
    """
    offenders: list[str] = []
    for subdir in _SCANNED_DIRS:
        root = src_root / subdir
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            rel = str(py.relative_to(src_root))
            if rel in _EXEMPT_RELATIVE_PATHS:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                target = _module_target(node) or ""
                for forbidden in _FORBIDDEN_IMPORTS:
                    if target == forbidden or target.startswith(forbidden + "."):
                        offenders.append(f"{rel}:{node.lineno} imports {target}")

    assert not offenders, (
        "Generic engine code must reach CDM state through ovid2.cdm.registry "
        "(P10 CDM/ETL independence):\n" + "\n".join(offenders)
    )
