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


#: Non-legacy pipeline packages (``legacy_ovid`` ports are Spark-only by
#: definition and governed by ``test_legacy_ports_contained.py``).
_PORTABLE_PIPELINES: tuple[str, ...] = (
    "apihosp",
    "dup",
    "acme_from_disc",
    "example_pcornet",
    "formhosp",
    "pcornet_to_omop",
)


def _declares_spark(text: str) -> bool:
    return 'REQUIRES_FAMILY = "spark"' in text or 'requires_family = "spark"' in text


@pytest.mark.contract
def test_portable_pipelines_import_pyspark_only_when_spark_family(repo_root: Path) -> None:
    """A non-legacy pipeline may import pyspark at runtime only if it declares Spark.

    Either the package (``REQUIRES_FAMILY = "spark"`` in
    ``pipeline_settings.py``) or the file (``requires_family = "spark"``)
    must say so; otherwise the pipeline claims to be engine-portable.
    """
    offenders: list[str] = []
    for name in _PORTABLE_PIPELINES:
        pkg = repo_root / "pipelines" / "ovid2_pipelines" / name
        settings = pkg / "pipeline_settings.py"
        if not settings.is_file():
            continue
        pkg_spark = _declares_spark(settings.read_text(encoding="utf-8"))
        for py in sorted(pkg.rglob("*.py")):
            if "__pycache__" in py.parts or "_superseded" in py.parts:
                continue
            lines = _runtime_pyspark_imports(py)
            if lines and not (pkg_spark or _declares_spark(py.read_text(encoding="utf-8"))):
                offenders += [f"{py.relative_to(repo_root)}:{n}" for n in lines]
    assert not offenders, (
        "Runtime pyspark import in an engine-portable pipeline — use "
        "ovid2.core.engines.dsl or declare REQUIRES_FAMILY/requires_family "
        '= "spark":\n' + "\n".join(offenders)
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
