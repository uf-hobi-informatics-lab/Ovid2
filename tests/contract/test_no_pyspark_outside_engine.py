"""Contract: pyspark and snowpark imports are confined to the engine layer.

The Ovid2 pipeline speaks to a compute backend through the
:class:`~ovid2.core.engines.protocols.EngineSession` and
:class:`~ovid2.core.engines.protocols.EngineFunctions` protocols
(:mod:`ovid2.core.engines.protocols`). Concrete backend imports
(``pyspark.*``, ``snowflake.snowpark.*``) live behind those protocols
in dedicated wrapper modules under :mod:`ovid2.core.engines`.

Two consequences this test guards:

1. **The web tier stays install-light.** ``ovid2 web`` and the MCP
   server must be importable without pyspark or snowpark on the
   system.
2. **The abstraction stays real.** If the pipeline imports
   ``pyspark.sql.functions`` directly, we cannot swap Spark for
   Snowpark by changing the engine; the abstraction becomes a lie.

The allow-list below is deliberately narrow: only the concrete engine
implementations and their session-adapter helpers may import from
``pyspark`` / ``snowflake.snowpark``. Additions require an ADR entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Modules that are ALLOWED to import ``pyspark.*`` **at runtime**.
#: ``if TYPE_CHECKING:`` imports are annotations only and need no entry
#: (pruned 2026-11-20 from 37 entries to these 7). Shrink-only:
#: ``test_pyspark_allowlist_entries_still_needed`` fails on a stale entry
#: and ``test_pyspark_allowlist_never_grows`` pins the count.
_PYSPARK_ALLOWED: frozenset[str] = frozenset(
    {
        # Concrete engine + wrappers.
        "src/ovid2/core/engines/pyspark.py",
        "src/ovid2/core/engines/databricks_spark.py",
        # Engine-dispatching column DSL: resolves to pyspark.sql.functions
        # only when the active engine is Spark-family (lazy import).
        "src/ovid2/core/engines/dsl.py",
        # Transitional — runtime pyspark still used; port to EngineFunctions.
        # See the adr-engines-resource-and-abstraction design note.
        "src/ovid2/core/pipeline.py",
        "src/ovid2/utils/common.py",
        "src/ovid2/transformations/deduplicators/base.py",
    }
)

#: Frozen upper bound on :data:`_PYSPARK_ALLOWED`. Lower it when an entry
#: is ported; never raise it without an ADR.
_PYSPARK_ALLOWED_MAX: int = 6

#: Directory-tree grace list. Every ``.py`` under one of these
#: directories is temporarily allowed to import pyspark. Each entry is
#: a scheduled follow-up port to :class:`EngineFunctions`.
_PYSPARK_ALLOWED_TREES: tuple[str, ...] = (
    # 23 common fixers. Ported to the engine-dispatching DSL
    # (``ovid2.core.engines.dsl``) with the DuckDB engine — the remaining
    # imports are ``TYPE_CHECKING``-only annotations;
    # ``test_no_runtime_pyspark_in_shared.py`` forbids runtime ones.
    "src/ovid2/transformations/fixers/common/",
    # Legacy-Ovid byte-parity layer (``ETL_PROFILE = "legacy_ovid"``).
    # Spark-only by definition: parity is measured against the legacy
    # Spark CSV writer/reader and legacy Python UDF evaluation. See
    # the adr-legacy-parity-port design note.
    "src/ovid2/legacy/",
)


def _is_driver_spark_module(rel: str) -> bool:
    """Return True for the D3 per-driver Spark data-plane modules.

    The Driver refactor's contract: pyspark imports live under
    ``src/ovid2/drivers/<type>/spark.py`` and nowhere else in the
    driver tree. Metadata / probe / driver / legacy modules must
    stay web-safe. As each ``core/addresses/<type>.py`` is migrated
    (D3.<type>), the corresponding legacy entry above is removed and
    the new ``drivers/<type>/spark.py`` picks up the allow-list via
    this predicate.
    """
    parts = rel.split("/")
    return (
        len(parts) == 5
        and parts[0] == "src"
        and parts[1] == "ovid2"
        and parts[2] == "drivers"
        and parts[4] == "spark.py"
    )


#: Modules that are ALLOWED to import ``snowflake.snowpark.*``.
_SNOWPARK_ALLOWED: frozenset[str] = frozenset(
    {
        "src/ovid2/core/engines/snowpark.py",
    }
)


def _is_snowpark_staging_strategy(rel: str) -> bool:
    """Return True for the Snowpark staging strategy modules.

    Phase C of the engine/storage-orthogonality plan introduces
    per-driver-type Snowpark staging strategies under
    ``src/ovid2/drivers/snowflake/staging/<type>.py`` (e.g.
    ``disc.py`` for the disc-source strategy). Each strategy calls
    into ``snowflake.snowpark.*`` for its ``PUT`` / ``GET`` /
    ``copy_into_location`` verbs. This is the analog of
    :func:`_is_driver_spark_module` for the Snowpark family — the
    ONLY place outside ``core/engines/snowpark.py`` allowed to
    touch ``snowflake.snowpark``.

    See ADR: the adr-engine-storage-orthogonality design note.
    """
    parts = rel.split("/")
    return (
        len(parts) == 6
        and parts[0] == "src"
        and parts[1] == "ovid2"
        and parts[2] == "drivers"
        and parts[3] == "snowflake"
        and parts[4] == "staging"
        and parts[5].endswith(".py")
        and parts[5] != "__init__.py"
    )


def _iter_py(root: Path):
    """Yield every ``.py`` under ``root``."""
    for py in root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        yield py


def _imports_root(module_name: str, roots: tuple[str, ...]) -> bool:
    """Return True if ``module_name`` starts with any of ``roots``."""
    return any(module_name == root or module_name.startswith(root + ".") for root in roots)


def _bad_imports(py: Path, forbidden_roots: tuple[str, ...]) -> list[str]:
    """Return ``line: module`` offenders for one file (runtime imports only).

    Uses the evasion-resistant walker: multi-name imports, ``from snowflake
    import snowpark`` (→ ``snowflake.snowpark``), relative imports and
    literal ``importlib.import_module`` / ``__import__``. Imports under
    ``if TYPE_CHECKING:`` are annotations and are ignored.
    """
    from tests.contract._ast_utils import dynamic_imports, iter_imports

    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    offenders: list[str] = []
    seen: set[tuple[int, str]] = set()
    for ref in iter_imports(tree):
        if ref.type_checking or not _imports_root(ref.module, forbidden_roots):
            continue
        if (ref.lineno, ref.module) not in seen:
            seen.add((ref.lineno, ref.module))
            offenders.append(f"{ref.lineno}: import {ref.module}")
    for dyn in dynamic_imports(tree):
        if dyn.module and _imports_root(dyn.module, forbidden_roots):
            offenders.append(f"{dyn.lineno}: import_module({dyn.module!r})")
    return offenders


@pytest.mark.contract
def test_pyspark_imports_confined_to_engine_layer(src_root: Path) -> None:
    """Only files on :data:`_PYSPARK_ALLOWED` may import ``pyspark.*``."""
    offenders: list[str] = []
    for py in _iter_py(src_root):
        rel = str(py.relative_to(src_root.parents[1]))
        if rel in _PYSPARK_ALLOWED:
            continue
        if any(rel.startswith(tree) for tree in _PYSPARK_ALLOWED_TREES):
            continue
        if _is_driver_spark_module(rel):
            continue
        bad = _bad_imports(py, ("pyspark",))
        for entry in bad:
            offenders.append(f"{rel}:{entry}")

    assert not offenders, (
        "pyspark imports found outside the engine layer. "
        "Route the call through engine.get_functions() / engine.get_session() "
        "instead, or add the file to _PYSPARK_ALLOWED with an ADR entry.\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_snowpark_imports_confined_to_engine_layer(src_root: Path) -> None:
    """Only files on :data:`_SNOWPARK_ALLOWED` may import ``snowflake.snowpark.*``."""
    offenders: list[str] = []
    for py in _iter_py(src_root):
        rel = str(py.relative_to(src_root.parents[1]))
        if rel in _SNOWPARK_ALLOWED:
            continue
        if _is_snowpark_staging_strategy(rel):
            continue
        bad = _bad_imports(py, ("snowflake.snowpark",))
        for entry in bad:
            offenders.append(f"{rel}:{entry}")

    assert not offenders, (
        "snowflake.snowpark imports found outside the engine layer. "
        "The pipeline speaks Snowpark through SnowparkEngine, never directly.\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_pyspark_allowlist_entries_still_needed(repo_root: Path) -> None:
    """Every allow-list entry must still exist and import pyspark at runtime."""
    stale = [
        rel
        for rel in sorted(_PYSPARK_ALLOWED)
        if not (repo_root / rel).is_file() or not _bad_imports(repo_root / rel, ("pyspark",))
    ]
    stale += [
        rel
        for rel in sorted(_SNOWPARK_ALLOWED)
        if not (repo_root / rel).is_file()
        or not _bad_imports(repo_root / rel, ("snowflake.snowpark",))
    ]
    assert not stale, (
        "Stale allow-list entries (no runtime import any more) — remove them and "
        "lower _PYSPARK_ALLOWED_MAX:\n" + "\n".join(stale)
    )


@pytest.mark.contract
def test_pyspark_allowlist_never_grows() -> None:
    """The per-file allow-list is shrink-only."""
    assert len(_PYSPARK_ALLOWED) <= _PYSPARK_ALLOWED_MAX, (
        f"_PYSPARK_ALLOWED grew to {len(_PYSPARK_ALLOWED)} (max {_PYSPARK_ALLOWED_MAX}). "
        "Route new code through ovid2.core.engines.dsl / EngineFunctions."
    )


@pytest.mark.contract
def test_pyspark_tree_grace_lists_only_shrink(repo_root: Path) -> None:
    """Runtime pyspark importers under the grace-list trees may only decrease."""
    from tests.contract._ratchet import check_ratchet

    counts: dict[str, int] = {}
    for tree in _PYSPARK_ALLOWED_TREES:
        for py in _iter_py(repo_root / tree):
            if _bad_imports(py, ("pyspark",)):
                counts[str(py.relative_to(repo_root))] = 1
    check_ratchet("pyspark_tree_runtime_importers", counts)


@pytest.mark.contract
def test_import_detector_catches_evasions(tmp_path: Path) -> None:
    """Negative control: aliasing, ``from X import Y`` and importlib are seen."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os, pyspark.sql as ps\n"
        "from snowflake import snowpark\n"
        "import importlib\n"
        "importlib.import_module('pyspark.sql.functions')\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import pyspark\n",
        encoding="utf-8",
    )
    spark = _bad_imports(bad, ("pyspark",))
    assert len(spark) == 2, spark  # aliased import + import_module; TYPE_CHECKING ignored
    assert _bad_imports(bad, ("snowflake.snowpark",))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
