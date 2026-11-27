"""Contract: every engine runs a pipeline's tables one at a time.

Engine consistency (2026-11-24): PySpark, Databricks Spark, Snowpark and
DuckDB all run tables sequentially, so the run page reports the same
order and per-table timings whatever the engine, and the running table
gets the engine's whole capacity. Pinned here:

* :data:`ovid2.core.table_runner.TABLE_CONCURRENCY` is ``1``;
* no engine exposes a ``table_concurrency`` knob;
* the table loops (``table_runner``, ``tools.copy``, ``tools.move``,
  ``legacy.runner``) never build a thread pool;
* the DuckDB container never receives a table-concurrency setting.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._ast_utils import alias_map, iter_imports, resolved_call_name

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"

#: Modules that own a per-table loop.
_TABLE_LOOPS = (
    "core/table_runner.py",
    "tools/copy.py",
    "tools/move.py",
    "legacy/runner.py",
)

_POOL_CALLS = frozenset(
    {
        "concurrent.futures.ThreadPoolExecutor",
        "concurrent.futures.ProcessPoolExecutor",
        "concurrent.futures.thread.ThreadPoolExecutor",
        "multiprocessing.Pool",
        "multiprocessing.pool.ThreadPool",
    }
)


def test_table_concurrency_is_one() -> None:
    from ovid2.core.table_runner import TABLE_CONCURRENCY

    assert TABLE_CONCURRENCY == 1


@pytest.mark.parametrize("rel", _TABLE_LOOPS)
def test_table_loops_build_no_pool(rel: str) -> None:
    path = _SRC / rel
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for ref in iter_imports(tree):
        assert not ref.module.startswith("concurrent.futures"), (
            f"{rel}:{ref.lineno} imports {ref.module}; tables run one at a time"
        )
    aliases = alias_map(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = resolved_call_name(node, aliases)
            assert name not in _POOL_CALLS, f"{rel}:{node.lineno} builds {name}"


def test_no_engine_has_table_concurrency() -> None:
    engines = _SRC / "core" / "engines"
    offenders = [
        p.name
        for p in sorted(engines.glob("*.py"))
        if "def table_concurrency" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"engines expose table_concurrency: {offenders}"


def test_no_table_concurrency_setting_anywhere() -> None:
    needles = ("TABLE_CONCURRENCY=", "OVID2_DUCKDB_TABLE_CONCURRENCY", '"table_concurrency"')
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "table_runner.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.relative_to(_SRC)}: {needle}")
    assert not offenders, offenders
