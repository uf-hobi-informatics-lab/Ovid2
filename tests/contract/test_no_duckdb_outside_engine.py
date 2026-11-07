"""Contract: ``import duckdb`` is confined to the DuckDB engine layer.

The DuckDB engine (``ovid2.core.engines.duckdb``) and its stager
(``ovid2.core.engines.duckdb_stager``) are the only modules that may
import the ``duckdb`` package. Per-driver DuckDB strategies
(``src/ovid2/drivers/<type>/duckdb.py``) talk to DuckDB *through* the
engine / stager helpers and must not import it either — that keeps the
driver tree importable on hosts without the wheel, and keeps the web /
MCP tiers (which import driver metadata) free of the dependency.

See ADR the adr-duckdb-engine design note.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_DUCKDB_ALLOWED: frozenset[str] = frozenset(
    {
        "src/ovid2/core/engines/duckdb.py",
        "src/ovid2/core/engines/duckdb_stager.py",
    }
)

#: Tiers that must never reach DuckDB even transitively through an
#: import of the engine module (web / MCP / config are web-safe).
_FORBIDDEN_IMPORTERS: tuple[str, ...] = ("src/ovid2/web/", "src/ovid2/mcp/", "src/ovid2/config/")


def _imports(py: Path) -> list[tuple[int, str]]:
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def _iter_py(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.contract
def test_duckdb_imports_confined_to_engine_layer(src_root: Path) -> None:
    """Only the engine + stager modules may ``import duckdb``."""
    offenders: list[str] = []
    for py in _iter_py(src_root):
        rel = str(py.relative_to(src_root.parents[1]))
        if rel in _DUCKDB_ALLOWED:
            continue
        for lineno, module in _imports(py):
            if module == "duckdb" or module.startswith("duckdb."):
                offenders.append(f"{rel}:{lineno}: {module}")
    assert not offenders, (
        "duckdb imports outside the DuckDB engine layer — route through "
        "DuckDBEngine / duckdb_stager helpers instead:\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_web_tiers_do_not_import_duckdb_engine(src_root: Path) -> None:
    """web / mcp / config never import the DuckDB engine or its strategies."""
    offenders: list[str] = []
    for py in _iter_py(src_root):
        rel = str(py.relative_to(src_root.parents[1]))
        if not rel.startswith(_FORBIDDEN_IMPORTERS):
            continue
        for lineno, module in _imports(py):
            if module in ("ovid2.core.engines.duckdb", "ovid2.core.engines.duckdb_stager") or (
                module.startswith("ovid2.drivers.") and module.endswith(".duckdb")
            ):
                offenders.append(f"{rel}:{lineno}: {module}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.contract
def test_every_duckdb_strategy_module_is_mapped() -> None:
    """Each ``drivers/<type>/duckdb.py`` is in the stager's module map, and vice versa."""
    from ovid2.core.engines.duckdb_stager import DuckDBEngineStager

    root = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "drivers"
    on_disk = {p.parent.name for p in root.glob("*/duckdb.py")}
    mapped = set(DuckDBEngineStager._STRATEGY_MODULES)
    assert on_disk == mapped, f"on disk {sorted(on_disk)} vs mapped {sorted(mapped)}"
    for driver_type, module in DuckDBEngineStager._STRATEGY_MODULES.items():
        assert module == f"ovid2.drivers.{driver_type}.duckdb"
