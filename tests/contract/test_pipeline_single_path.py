"""Contract: ``core/pipeline.py`` has one execution path for every engine.

The pipeline used to branch each table into a Spark leg and a staged
(non-Spark) leg. Engine-specific steps are now engine / stager methods
(``register_temp_view``, ``warm_reference``, ``read_shared_view_file``,
``prepare_workspace``, ``release_checkpoints``,
``EngineStager.accepts_legacy_address``). This test forbids the branch from
coming back:

* no reference to ``session_is_spark`` / ``family`` / ``EngineFamily`` /
  ``supports_*`` capability flags in ``core/pipeline.py``;
* no ``_run_pipeline_spark`` / ``_run_pipeline_staged`` / dispatch helper;
* no runtime ``pyspark`` import (``TYPE_CHECKING`` only).
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.contract._ast_utils import iter_imports

_FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "session_is_spark",
        "family",
        "EngineFamily",
        "supports_pandas_udf",
        "supports_broadcast_hint",
        "supports_input_file_name",
        "supports_local_files",
    }
)

_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {"_run_pipeline_spark", "_run_pipeline_staged", "_run_pipeline_dispatch", "_needs_staged_spark"}
)


def _tree(src_root: Path) -> ast.Module:
    return ast.parse((src_root / "core" / "pipeline.py").read_text(encoding="utf-8"))


def test_pipeline_does_not_branch_on_engine(src_root: Path) -> None:
    hits = [
        f"line {node.lineno}: {node.attr if isinstance(node, ast.Attribute) else node.id}"
        for node in ast.walk(_tree(src_root))
        if (isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS)
        or (isinstance(node, ast.Name) and node.id in _FORBIDDEN_ATTRS)
        or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _FORBIDDEN_ATTRS
        )
    ]
    assert not hits, (
        "core/pipeline.py branches on the engine; move the difference behind an "
        "engine / stager method:\n" + "\n".join(hits)
    )


def test_pipeline_has_one_path(src_root: Path) -> None:
    names = {
        node.name
        for node in ast.walk(_tree(src_root))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "_run_pipeline" in names
    assert not names & _FORBIDDEN_FUNCTIONS, sorted(names & _FORBIDDEN_FUNCTIONS)


def test_pipeline_imports_no_pyspark_at_runtime(src_root: Path) -> None:
    runtime = [
        ref.module
        for ref in iter_imports(_tree(src_root), "ovid2.core")
        if not ref.type_checking and ref.module.split(".")[0] == "pyspark"
    ]
    assert not runtime, runtime


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("pytest contract module; not executable.")
