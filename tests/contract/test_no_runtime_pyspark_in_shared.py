"""Contract: shared transformation code never imports pyspark at runtime.

Fixers, mapper ops, analysis passes and the formatter adapters run on
every engine family (PySpark, DuckDB, Snowpark). They build column
expressions through :mod:`ovid2.core.engines.dsl` (which resolves to the
active engine's functions) and may import ``pyspark`` only for type
annotations under ``if TYPE_CHECKING:``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Trees / files that must stay engine-portable.
_SHARED: tuple[str, ...] = (
    "transformations/fixers/common/",
    "mappers/ops/",
    "transformations/analysis/",
    "transformations/quality/mapping_gap.py",
    "formatters/adapters/_sources.py",
    "formatters/adapters/semantic_join.py",
)


def _runtime_pyspark_imports(py: Path) -> list[int]:
    tree = ast.parse(py.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            for inner in ast.walk(node):
                guarded.add(id(inner))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if (
            isinstance(node, ast.Import)
            and any(a.name.split(".")[0] == "pyspark" for a in node.names)
        ) or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pyspark"):
            lines.append(node.lineno)
    return lines


@pytest.mark.contract
def test_no_runtime_pyspark_in_shared_code(src_root: Path) -> None:
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        rel = str(py.relative_to(src_root))
        if "__pycache__" in py.parts or not rel.startswith(_SHARED):
            continue
        offenders.extend(f"{rel}:{ln}" for ln in _runtime_pyspark_imports(py))
    assert not offenders, (
        "Runtime pyspark imports in engine-portable code — import column "
        "functions from ovid2.core.engines.dsl instead:\n" + "\n".join(offenders)
    )
