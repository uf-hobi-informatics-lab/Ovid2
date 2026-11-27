"""Contract: shared transformation code never imports pyspark at runtime.

Fixers, mapper ops, analysis passes and the formatter adapters run on
every engine family (PySpark, DuckDB, Snowpark). They build column
expressions through :mod:`ovid2.core.engines.dsl` (which resolves to the
active engine's functions) and may import ``pyspark`` only for type
annotations under ``if TYPE_CHECKING:``.
"""

from __future__ import annotations

import ast
import warnings
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


def test_no_pipeline_package_imports_pyspark(repo_root: Path) -> None:
    """No pipeline package — hand-written or generated legacy port — imports pyspark at runtime.

    Every engine runs every pipeline (engine universality,
    ``adr-engine-universality.md``): column functions come from
    :mod:`ovid2.core.engines.dsl`, generated ports import the portable
    :mod:`ovid2.core.engines.sparkapi` package (the legacy formatter
    migrator rewrites their ``pyspark`` imports). There is no family
    marker that exempts a package any more.
    """
    import warnings

    offenders: list[str] = []
    root = repo_root / "pipelines" / "ovid2_pipelines"
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts or "_superseded" in py.parts:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            lines = _runtime_pyspark_imports(py)
        offenders += [f"{py.relative_to(repo_root)}:{n}" for n in lines]
    assert not offenders, (
        "Runtime pyspark import in a pipeline package — use ovid2.core.engines.dsl "
        "(hand-written) or re-run the legacy formatter migrator (generated ports):\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_pipelines_do_not_import_each_other(repo_root: Path) -> None:
    """A pipeline package must not import another pipeline package (ratchet)."""
    from tests.contract._ast_utils import iter_imports
    from tests.contract._ratchet import check_ratchet

    root = repo_root / "pipelines" / "ovid2_pipelines"
    counts: dict[str, int] = {}
    for pkg in sorted(p for p in root.iterdir() if (p / "pipeline_settings.py").is_file()):
        for py in pkg.rglob("*.py"):
            if "__pycache__" in py.parts or "_superseded" in py.parts:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)  # verbatim legacy escapes
                tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
            # ``from ovid2_pipelines.<other> import x`` / ``import ovid2_pipelines.<other>``.
            n = sum(
                1
                for ref in iter_imports(tree)
                if ref.module.startswith("ovid2_pipelines.")
                and ref.module.split(".")[1] != pkg.name
            )
            if n:
                counts[str(py.relative_to(repo_root))] = n
    check_ratchet("cross_pipeline_imports", counts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
