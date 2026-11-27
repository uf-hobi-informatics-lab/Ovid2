"""Contract: no engine-family gate may come back (engine universality).

History: pipelines, formatters and transforms could declare
``REQUIRES_FAMILY`` / ``requires_family`` to refuse an engine, and the web
tier, CLI, runner and UI enforced it ("Pipeline 'acme' needs a 'spark'
engine"). Every engine now runs every pipeline on every storage type
(``adr-engine-universality.md``); a feature an engine lacks is added to the
engine (:mod:`ovid2.core.engines.surface`), never gated.

This test forbids, across ``src/``, ``pipelines/ovid2_pipelines`` and the
web templates / JS:

* declaring ``REQUIRES_FAMILY`` or a ``requires_family`` attribute / key;
* the removed gate entry points (``engine_family_mismatch``,
  ``assert_pipeline_engine_compatible``, ``resolve_pipeline_family``,
  ``required_engine_family``, ``_assert_formatter_family``,
  ``TransformFamilyError``) and their modules;
* UI filtering by a pipeline's family (``data-requires-family``,
  ``pipeline_families``, ``applyEngineFamily``).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "REQUIRES_FAMILY",
        "requires_family",
        "engine_family_mismatch",
        "assert_pipeline_engine_compatible",
        "resolve_pipeline_family",
        "required_engine_family",
        "_assert_formatter_family",
        "TransformFamilyError",
    }
)

_FORBIDDEN_MODULES: tuple[str, ...] = (
    "ovid2.core.engines.family",
    "ovid2.config.pipeline_family",
)

_UI_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"data-requires-family"),
    re.compile(r"\bpipeline_families\b"),
    re.compile(r"\bapplyEngineFamily\b"),
    re.compile(r"\brequiresFamily\b"),
    re.compile(r"\brequires_family\b"),
)


def _python_files(repo_root: Path) -> list[Path]:
    roots = [repo_root / "src" / "ovid2", repo_root / "pipelines" / "ovid2_pipelines"]
    out: list[Path] = []
    for root in roots:
        out += [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    return out


def _names(tree: ast.AST) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            hits.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            hits.append((node.attr, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _FORBIDDEN_NAMES:
            hits.append((node.name, node.lineno))
        elif isinstance(node, ast.Constant) and node.value in _FORBIDDEN_NAMES:
            # Dict keys / getattr strings ("requires_family").
            hits.append((str(node.value), node.lineno))
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in _FORBIDDEN_NAMES:
            hits.append((node.name, getattr(node, "lineno", 0)))
    return hits


def test_no_family_gate_in_python(repo_root: Path) -> None:
    import warnings

    offenders = []
    for py in _python_files(repo_root):
        with warnings.catch_warnings():
            # Verbatim legacy dictionaries carry invalid string escapes.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for name, line in _names(tree):
            offenders.append(f"{py.relative_to(repo_root)}:{line} {name}")
    assert not offenders, (
        "Engine-family gating is forbidden (every engine runs every pipeline — "
        "implement the missing operation on the engine instead):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("module", _FORBIDDEN_MODULES)
def test_gate_modules_are_gone(module: str) -> None:
    assert importlib.util.find_spec(module) is None, f"{module} must not exist"


def test_no_family_filtering_in_ui(repo_root: Path) -> None:
    web = repo_root / "src" / "ovid2" / "web"
    offenders = []
    for path in [*web.rglob("*.html"), *web.rglob("*.js")]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in _UI_PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(repo_root)}:{lineno} {pattern.pattern}")
    assert not offenders, "UI must not hide engines by pipeline family:\n" + "\n".join(offenders)


def test_detector_negative_control(tmp_path: Path) -> None:
    bad = tmp_path / "x.py"
    bad.write_text(
        'REQUIRES_FAMILY = "spark"\n'
        "class F:\n    requires_family = 'spark'\n"
        "x = {'requires_family': None}\n"
        "from ovid2.config.pipeline_family import engine_family_mismatch\n",
        encoding="utf-8",
    )
    hits = {n for n, _ in _names(ast.parse(bad.read_text()))}
    assert {"REQUIRES_FAMILY", "requires_family", "engine_family_mismatch"} <= hits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
