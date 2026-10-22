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

#: Modules that are ALLOWED to import ``pyspark.*``. Anything else
#: MUST go through :mod:`ovid2.core.engines.protocols` and pull
#: functions from ``engine.get_functions()``.
_PYSPARK_ALLOWED: frozenset[str] = frozenset(
    {
        # Concrete engine + wrappers.
        "src/ovid2/core/engines/pyspark.py",
        "src/ovid2/core/engines/databricks_spark.py",
        # Transitional grace list — these files predate the abstraction
        # and get ported incrementally. Every entry below is a call-out
        # for the follow-up "port to EngineFunctions" pass; do NOT add
        # new ones without an ADR. See
        # the adr-engines-resource-and-abstraction design note.
        "src/ovid2/core/pipeline.py",
        "src/ovid2/core/addresses/base.py",
        # ``core/addresses/disc.py`` is intentionally absent —
        # migrated to ``drivers/disc/spark.py`` under D3.disc.
        # ``core/addresses/snowflake.py`` is intentionally absent —
        # migrated to ``drivers/snowflake/spark.py`` under D3.snowflake.
        # ``core/addresses/azure_blob.py`` is intentionally absent —
        # migrated to ``drivers/azure_blob/spark.py`` under D3.azure_blob.
        # ``core/addresses/s3.py`` is intentionally absent —
        # migrated to ``drivers/s3/spark.py`` under D3.s3.
        "src/ovid2/utils/common.py",
        "src/ovid2/utils/geocoding.py",
        "src/ovid2/formatters/engine.py",
        "src/ovid2/formatters/base.py",
        "src/ovid2/formatters/adapters/base.py",
        "src/ovid2/formatters/adapters/_sources.py",
        "src/ovid2/formatters/adapters/semantic_join.py",
        "src/ovid2/formatters/adapters/source_passthrough.py",
        "src/ovid2/formatters/adapters/custom.py",
        "src/ovid2/formatters/adapters/union_of_specs.py",
        "src/ovid2/mappers/base.py",
        "src/ovid2/mappers/engine.py",
        "src/ovid2/mappers/ops/passthrough.py",
        "src/ovid2/mappers/ops/literal.py",
        "src/ovid2/mappers/ops/case_map.py",
        "src/ovid2/mappers/ops/hash_id.py",
        "src/ovid2/mappers/ops/regex_replace.py",
        "src/ovid2/mappers/ops/translate_by_dict.py",
        "src/ovid2/mappers/ops/translate_by_concept.py",
        "src/ovid2/transformations/base.py",
        "src/ovid2/transformations/deduplicate.py",
        "src/ovid2/transformations/format.py",
        "src/ovid2/transformations/map.py",
        "src/ovid2/transformations/fix.py",
        "src/ovid2/transformations/mappers/base.py",
        "src/ovid2/transformations/deduplicators/base.py",
        "src/ovid2/transformations/fixers/base.py",
        "src/ovid2/transformations/quality/mapping_report.py",
        "src/ovid2/transformations/quality/mapping_gap.py",
        "src/ovid2/transformations/quality/runner.py",
        "src/ovid2/transformations/quality/ops/__init__.py",
        "src/ovid2/transformations/analysis/distinct_values.py",
        "src/ovid2/transformations/analysis/monthly_counts.py",
    }
)

#: Directory-tree grace list. Every ``.py`` under one of these
#: directories is temporarily allowed to import pyspark. Each entry is
#: a scheduled follow-up port to :class:`EngineFunctions`.
_PYSPARK_ALLOWED_TREES: tuple[str, ...] = (
    # 23 common fixers — currently Spark-family only. To be ported by
    # threading ``ctx["functions"]`` through the fixer context.
    "src/ovid2/transformations/fixers/common/",
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
    """Return a list of ``line: module`` offenders for one file."""
    text = py.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(py))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _imports_root(alias.name, forbidden_roots):
                    offenders.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _imports_root(module, forbidden_roots):
                names = ", ".join(a.name for a in node.names)
                offenders.append(f"{node.lineno}: from {module} import {names}")
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
