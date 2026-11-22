"""Contract: no eager Spark actions in engine code.

Enforces the lazy-Spark invariant (``AGENTS.md`` §Core design principles):
``core/``, ``transformations/``, ``transforms/``, ``mappers/``,
``formatters/`` and ``semantic/`` must never call the following methods,
which materialize a DataFrame and defeat lazy execution:

* ``.collect()``
* ``.toPandas()``
* ``.take(...)``
* ``.first()``
* ``.count()``

Also forbidden (2026-11-20): ``.head()``, ``.tail()``, ``.show()``,
``.toLocalIterator()``, ``.foreach()``, ``.foreachPartition()``; a bare
reference (``f = df.collect``) and ``getattr(df, "collect")``. The six
non-legacy pipeline packages are scanned too (``legacy_ovid`` ports are
governed by ``test_legacy_ports_contained.py``).

**Exceptions** — a line carrying ``# noqa: OVID2001 <reason>`` is
intentionally allowed; the reason (≥ 3 words, after an optional ``—``) is
mandatory. The total number of suppressions is a shrink-only ratchet
(``data/shrink_only.json["ovid2001_suppressions"]``).

The scanner uses the Python AST so mentions in docstrings, string
literals, and comments do not count.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

FORBIDDEN_METHODS: frozenset[str] = frozenset(
    {
        "collect",
        "toPandas",
        "take",
        "first",
        "count",
        "head",
        "tail",
        "show",
        "toLocalIterator",
        "foreach",
        "foreachPartition",
    }
)

#: Non-legacy pipeline packages held to the same rule (repo-relative).
SCANNED_PIPELINES: tuple[str, ...] = (
    "apihosp",
    "dup",
    "acme_from_disc",
    "example_pcornet",
    "formhosp",
    "pcornet_to_omop",
)

SUPPRESSION_MARKER: str = "OVID2001"
SCANNED_DIRS: tuple[str, ...] = (
    "core",
    "transformations",
    "transforms",
    "mappers",
    "formatters",
    "semantic",
)


_REASON_RE = re.compile(r"OVID2001\s*(?:[—:-]\s*)?(?P<reason>.*)$")


def _suppression(source_lines: list[str], line_no: int) -> str | None:
    """Return the suppression reason on ``line_no`` ("" when bare), or ``None``."""
    if not 1 <= line_no <= len(source_lines):
        return None
    line = source_lines[line_no - 1]
    if SUPPRESSION_MARKER not in line:
        return None
    match = _REASON_RE.search(line)
    return (match.group("reason") if match else "").strip()


def _reason_ok(reason: str) -> bool:
    return len(reason.split()) >= 3


def _scan_module(py: Path, base: Path) -> tuple[list[str], int]:
    """Return ``(offenders, suppression_count)`` for one file."""
    from tests.contract._ast_utils import getattr_literals

    text = py.read_text(encoding="utf-8")
    source_lines = text.splitlines()
    tree = ast.parse(text, filename=str(py))
    rel = py.relative_to(base).as_posix()

    offenders: list[str] = []
    suppressed = 0

    def _hit(line: int, what: str) -> None:
        nonlocal suppressed
        reason = _suppression(source_lines, line)
        if reason is None:
            offenders.append(f"{rel}:{line} {what}")
        elif not _reason_ok(reason):
            offenders.append(f"{rel}:{line} {what} — OVID2001 needs a reason (≥ 3 words)")
        else:
            suppressed += 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_METHODS:
            # Any attribute access — called or referenced (``f = df.collect``).
            _hit(node.lineno, f".{node.attr}")
    for attr, line in getattr_literals(tree):
        if attr in FORBIDDEN_METHODS:
            _hit(line, f"getattr(…, {attr!r})")
    return offenders, suppressed


def _scan_all(repo_root: Path) -> tuple[list[str], dict[str, int]]:
    src_root = repo_root / "src" / "ovid2"
    roots = [src_root / d for d in SCANNED_DIRS] + [
        repo_root / "pipelines" / "ovid2_pipelines" / p for p in SCANNED_PIPELINES
    ]
    offenders: list[str] = []
    counts: dict[str, int] = {}
    for root in roots:
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            bad, n = _scan_module(py, repo_root)
            offenders += bad
            if n:
                counts[py.relative_to(repo_root).as_posix()] = n
    return offenders, counts


@pytest.mark.contract
def test_no_collect_in_engine_sources(repo_root: Path) -> None:
    """No engine module or non-legacy pipeline may invoke an eager Spark action."""
    offenders, _counts = _scan_all(repo_root)
    assert not offenders, (
        "Eager Spark action found in engine code (violates lazy-Spark rule):\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_ovid2001_suppressions_only_shrink(repo_root: Path) -> None:
    """The number of ``# noqa: OVID2001`` waivers may only decrease."""
    from tests.contract._ratchet import check_ratchet

    _offenders, counts = _scan_all(repo_root)
    check_ratchet("ovid2001_suppressions", counts)


@pytest.mark.contract
def test_detector_catches_evasions(tmp_path: Path) -> None:
    """Negative control: reference, getattr, new actions, bare waivers."""
    py = tmp_path / "m.py"
    py.write_text(
        "f = df.collect\n"
        "getattr(df, 'toPandas')()\n"
        "df.show()\n"
        "df.count()  # noqa: OVID2001\n"
        "df.first()  # noqa: OVID2001 — deliberate tiny lookup\n",
        encoding="utf-8",
    )
    offenders, suppressed = _scan_module(py, tmp_path)
    text = "\n".join(offenders)
    assert ".collect" in text and "toPandas" in text and ".show" in text
    assert "needs a reason" in text
    assert suppressed == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
