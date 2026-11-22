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

Imports are checked with the shared evasion-resistant walker
(``tests/contract/_ast_utils``: multi-name imports, ``from ovid2.cdm import
pcornet_7_0``, literal ``importlib.import_module``). The literal ban is a
**shrink-only ratchet**: PCORnet table / column names (from the CDM YAML,
≥ 4 chars) in executable code (docstrings excluded) are counted per file
and may never exceed ``data/shrink_only.json["pcornet_literals"]``. Fixers
whose ``FIXER_SCOPES`` entry names ``pcornet_7_0`` are exempt.
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


def _targets(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(line, module)`` for static and literal dynamic imports."""
    from tests.contract._ast_utils import dynamic_imports, iter_imports

    out = [(r.lineno, r.module) for r in iter_imports(tree)]
    out += [(d.lineno, d.module) for d in dynamic_imports(tree) if d.module]
    return out


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
            for lineno, target in _targets(tree):
                for forbidden in _FORBIDDEN_IMPORTS:
                    if target == forbidden or target.startswith(forbidden + "."):
                        offenders.append(f"{rel}:{lineno} imports {target}")

    assert not offenders, (
        "Generic engine code must reach CDM state through ovid2.cdm.registry "
        "(P10 CDM/ETL independence):\n" + "\n".join(offenders)
    )


def _pcornet_names(src_root: Path) -> frozenset[str]:
    """Return PCORnet table + column names (upper-case, ≥ 4 chars) from the CDM YAML."""
    import yaml

    names: set[str] = set()
    for table in (src_root / "resources" / "cdms" / "pcornet_7_0" / "tables").glob("*.yaml"):
        names.add(table.stem.upper())
        doc = yaml.safe_load(table.read_text(encoding="utf-8")) or {}
        for field in doc.get("fields") or []:
            if isinstance(field, dict) and field.get("name"):
                names.add(str(field["name"]).upper())
    return frozenset(n for n in names if len(n) >= 4)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def pcornet_literal_counts(src_root: Path) -> dict[str, int]:
    """Return ``{relative_path: count}`` of PCORnet name literals in generic code."""
    from ovid2.transformations.fixers.common.registry import FIXER_SCOPES

    names = _pcornet_names(src_root)
    scoped = {k for k, v in FIXER_SCOPES.items() if v != "generic"}
    counts: dict[str, int] = {}
    for subdir in _SCANNED_DIRS:
        for py in (src_root / subdir).rglob("*.py"):
            rel = py.relative_to(src_root).as_posix()
            if rel in _EXEMPT_RELATIVE_PATHS or ("fixers/common/" in rel and py.stem in scoped):
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            docs = _docstring_nodes(tree)
            n = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in names
                and id(node) not in docs
            )
            if n:
                counts[f"src/ovid2/{rel}"] = n
    return counts


@pytest.mark.contract
def test_pcornet_literals_only_shrink(src_root: Path) -> None:
    """PCORnet schema literals in generic code may only decrease (ratchet)."""
    from tests.contract._ratchet import check_ratchet

    check_ratchet("pcornet_literals", pcornet_literal_counts(src_root))


@pytest.mark.contract
def test_import_detector_catches_from_cdm_import() -> None:
    """Negative control: ``from ovid2.cdm import pcornet_7_0`` is seen."""
    tree = ast.parse("import os, ovid2.cdm.omop_5_3\nfrom ovid2.cdm import pcornet_7_0\n")
    targets = {t for _l, t in _targets(tree)}
    assert {"ovid2.cdm.pcornet_7_0", "ovid2.cdm.omop_5_3"} <= targets


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
