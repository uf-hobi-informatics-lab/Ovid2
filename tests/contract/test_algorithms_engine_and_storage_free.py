"""Contract: algorithms are engine-free and storage-free.

Algorithms (formatters, mapper ops, transforms, fixers, deduplicators,
health checks — everything under ``src/ovid2/{formatters,mappers,transforms,
transformations,algorithms}/``) may depend only on the engine-neutral API
(:mod:`ovid2.core.engines.dsl` / :mod:`ovid2.core.engines.protocols`) and on
what their context hands them. They never reach for:

* a compute backend — ``pyspark``, ``snowflake`` (Snowpark), ``duckdb``, or a
  concrete engine module (``ovid2.core.engines.{pyspark,databricks_spark,
  snowpark,duckdb,duckdb_stager,_duck*}``);
* a storage backend — ``ovid2.drivers`` / ``ovid2.connections``. Storage is
  resolved by the pipeline and handed over as a
  :class:`~ovid2.core.sources.SourceHandle` (``AdapterContext.source``) or a
  reader callable.

Module-level *and* lazy (in-function) imports count, as do
``importlib.import_module("…")`` literals. ``if TYPE_CHECKING:`` imports are
annotations only and allowed. Any leftover offender lives in the shrink-only
ratchet ``algorithm_backend_imports`` (``data/shrink_only.json``) — never a
free-form allow-list.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.contract._ast_utils import dynamic_imports, iter_imports
from tests.contract._ratchet import check_ratchet

#: Algorithm trees (relative to ``src/ovid2``).
ALGORITHM_TREES: tuple[str, ...] = (
    "formatters",
    "mappers",
    "transforms",
    "transformations",
    "algorithms",
)

#: Backend packages an algorithm must never import (prefix match).
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "pyspark",
    "snowflake",
    "duckdb",
    "ovid2.drivers",
    "ovid2.connections",
    "ovid2.core.engines.pyspark",
    "ovid2.core.engines.databricks_spark",
    "ovid2.core.engines.snowpark",
    "ovid2.core.engines.duckdb",
    "ovid2.core.engines.duckdb_stager",
)


def is_forbidden(module: str) -> bool:
    """Return whether ``module`` is a compute / storage backend import."""
    if module.startswith("ovid2.core.engines._duck"):
        return True
    return any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)


def _package_of(path: Path, src_root: Path) -> str:
    rel = path.relative_to(src_root.parent).with_suffix("")
    # ``pkg/mod.py`` and ``pkg/__init__.py`` both resolve relative imports
    # against ``pkg``.
    return ".".join(rel.parts[:-1])


def backend_import_counts(src_root: Path) -> dict[str, int]:
    """Return ``{repo-relative path: offending import lines}`` across algorithm trees."""
    counts: dict[str, int] = {}
    repo = src_root.parents[1]
    for tree_name in ALGORITHM_TREES:
        base = src_root / tree_name
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            lines: set[int] = set()
            for ref in iter_imports(tree, _package_of(py, src_root)):
                if not ref.type_checking and is_forbidden(ref.module):
                    lines.add(ref.lineno)
            for dyn in dynamic_imports(tree):
                if dyn.module is not None and is_forbidden(dyn.module):
                    lines.add(dyn.lineno)
            if lines:
                counts[str(py.relative_to(repo))] = len(lines)
    return counts


def test_algorithms_import_no_engine_or_storage_backend(src_root: Path) -> None:
    """Every algorithm tree is free of backend imports (shrink-only ratchet)."""
    check_ratchet("algorithm_backend_imports", backend_import_counts(src_root))


def test_detector_catches_lazy_aliased_and_dynamic_imports(tmp_path: Path) -> None:
    """The scanner sees through spellings a naive grep would miss."""
    src_root = tmp_path / "src" / "ovid2"
    pkg = src_root / "transformations"
    pkg.mkdir(parents=True)
    (pkg / "leaky.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from pyspark.sql import DataFrame\n"  # allowed
        "import importlib\n"
        "def f():\n"
        "    import duckdb as d\n"  # lazy + alias
        "    from ovid2.drivers.registry import driver_from_address\n"
        "    from ovid2.core.engines import pyspark as ps\n"  # from-pkg-import-sub
        "    importlib.import_module('ovid2.connections.disc')\n"  # dynamic
        "    from ovid2.core.engines.dsl import col\n",  # allowed
        encoding="utf-8",
    )
    counts = backend_import_counts(src_root)
    assert counts == {"src/ovid2/transformations/leaky.py": 4}


def test_source_handle_replaces_concrete_address_checks(src_root: Path) -> None:
    """Formatter adapters ask the handle (``is_local_path``), never ``isinstance``."""
    text = (src_root / "formatters" / "adapters" / "_sources.py").read_text(encoding="utf-8")
    assert "DiscAddress" not in text
    assert "is_local_path" in text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("pytest contract module; not executable.")
