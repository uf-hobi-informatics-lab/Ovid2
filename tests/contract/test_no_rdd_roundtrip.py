"""Contract: no ``DataFrame.rdd`` round-trips in shipped data-plane code.

``spark.createDataFrame(df.rdd, schema)`` (or any ``.rdd`` access on a
DataFrame) converts the JVM plan into a Python RDD: every row is pickled
JVM -> Python -> JVM through Python worker processes. It was the dominant
cost of PySpark -> Snowflake writes until 2026-10 (see CHANGELOG). The
same effect is achievable with a JVM-side projection
(``col.alias(name, metadata=...)``, ``DataFrame.to(schema)``).

Scans ``src/ovid2/`` and ``pipelines/`` with the AST so docstrings and
comments do not count. A line ending in ``# noqa: OVID2004`` is
exempted (justify it in the same commit).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_SCAN_ROOTS: tuple[Path, ...] = (_REPO_ROOT / "src" / "ovid2", _REPO_ROOT / "pipelines")
_MARKER: str = "OVID2004"


#: Generated verbatim legacy-Ovid ports (``legacy_ovid`` profile) keep the
#: legacy code unchanged by design; containment is enforced by
#: ``test_legacy_ports_contained.py``.
_LEGACY_PORT_MARKER: str = "ovid2: legacy-port"


def _offenders(py: Path) -> list[str]:
    text = py.read_text(encoding="utf-8")
    if ".rdd" not in text:
        return []  # fast path; also skips huge generated dictionaries
    if _LEGACY_PORT_MARKER in text[:2000]:
        return []
    lines = text.splitlines()
    out: list[str] = []
    for node in ast.walk(ast.parse(text, filename=str(py))):
        if isinstance(node, ast.Attribute) and node.attr == "rdd":
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if _MARKER not in line:
                out.append(f"{py.relative_to(_REPO_ROOT)}:{node.lineno}")
    return out


@pytest.mark.contract
def test_no_rdd_roundtrip() -> None:
    offenders: list[str] = []
    for root in _SCAN_ROOTS:
        for py in root.rglob("*.py"):
            offenders.extend(_offenders(py))
    assert not offenders, (
        "DataFrame.rdd access found (pickles every row through Python):\n" + "\n".join(offenders)
    )
