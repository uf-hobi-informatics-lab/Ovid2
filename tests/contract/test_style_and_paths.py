"""Contract: no hardcoded ``/app``, ``/data``, ``/ovid_credentials`` paths in source.

Runtime paths must be resolved via :mod:`ovid2.config.settings` (which
honors env vars and XDG conventions). Compose YAML strings that construct
mount specs like ``f"{DATA_DIR}:/data:ro"`` are fine — the container-side
part of the mount is unavoidable.

The test scans string literals with an AST walker so hits inside comments
and docstrings do not count. Certain modules are explicitly exempt: the
docker/compose modules must reference the container-side paths as string
literals to construct mount specs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# Modules that need to reference container-side paths as string literals.
EXEMPT_MODULES: frozenset[str] = frozenset(
    {
        "core/docker.py",
        "core/compose.py",
        # Authoritative registry of the standard container mount points.
        # The ``RESERVED_CONTAINER_PATHS`` frozenset must name each path
        # as a literal so ``parse_extra_mount`` can reject shadowing
        # attempts.
        "tools/extra_mounts.py",
        # Driver-side twin of ``tools/extra_mounts.py``:
        # ``RESERVED_CONTAINER_PATHS`` + ``FORBIDDEN_CONTAINER_PATHS``
        # on the driver-based collector must name each protected mount
        # target as a literal so a driver cannot smuggle an
        # auto-mount onto ``/data`` or ``/`` uniformly across every
        # peripheral type.
        "drivers/collector.py",
    }
)

# Any literal starting with one of these prefixes trips the check.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "/app/",
    "/data/",
    "/ovid_credentials/",
    "/opt/bitnami/",
    # Runtime-image ``$SPARK_HOME`` — only ``core/docker.py`` (which owns
    # ``SPARK_HOME_IN_CONTAINER``) and ``core/compose.py`` may spell it.
    "/opt/spark/",
)
# Bare forms (``"/app"``, ``"/data"``, ``"/ovid_credentials"``) count too.
FORBIDDEN_EXACT: frozenset[str] = frozenset(
    {
        "/app",
        "/data",
        "/ovid_credentials",
        "/opt/spark",
    }
)

_SUPPRESSION_MARKER: str = "OVID2002"


def _line_has_suppression(source_lines: list[str], line_no: int) -> bool:
    if 1 <= line_no <= len(source_lines):
        return _SUPPRESSION_MARKER in source_lines[line_no - 1]
    return False


@pytest.mark.contract
def test_no_hardcoded_container_paths(src_root: Path) -> None:
    """Reject hardcoded container mount paths in Python source."""
    offenders: list[str] = []

    for py in src_root.rglob("*.py"):
        rel = str(py.relative_to(src_root))
        if rel in EXEMPT_MODULES:
            continue

        text = py.read_text(encoding="utf-8")
        source_lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(py))
        except SyntaxError as exc:
            pytest.fail(f"Failed to parse {py}: {exc}")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if _line_has_suppression(source_lines, node.lineno):
                continue
            if value in FORBIDDEN_EXACT or any(value.startswith(p) for p in FORBIDDEN_PREFIXES):
                offenders.append(f"{rel}:{node.lineno} {value!r}")

    assert not offenders, (
        "Hardcoded container/host paths in source (AGENTS.md §Style rules):\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_no_fstring_sql(src_root: Path) -> None:
    """Reject f-string SQL construction in engine code.

    Heuristic: any f-string whose value contains ``SELECT ``, ``INSERT INTO``,
    ``UPDATE ``, ``DELETE FROM``, ``CREATE TABLE`` or similar is flagged.
    Parameterised SQL only — use connector ``execute(sql, params)``.

    Two exemptions:

    - Lines carrying ``# noqa: OVID2003`` are ignored one at a time.
    - Files whose module-level docstring contains ``ovid2: allow-sql-fstring``
      are skipped entirely (used by the batch tracker, whose f-strings
      interpolate trusted class-level identifiers only).
    """
    sql_pattern = re.compile(
        r"\b(SELECT\s+|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
        r"CREATE\s+TABLE|DROP\s+TABLE|MERGE\s+INTO)",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        source_lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(py))
        except SyntaxError as exc:
            pytest.fail(f"Failed to parse {py}: {exc}")

        module_doc = ast.get_docstring(tree) or ""
        if "ovid2: allow-sql-fstring" in module_doc:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            # Concatenate the string constants inside the f-string.
            joined = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if not sql_pattern.search(joined):
                continue
            line_text = (
                source_lines[node.lineno - 1] if 1 <= node.lineno <= len(source_lines) else ""
            )
            if "OVID2003" in line_text:
                continue
            offenders.append(f'{py.relative_to(src_root)}:{node.lineno} f"...{joined[:60]}..."')

    assert not offenders, (
        "F-string SQL detected — use parameterised queries instead "
        "(AGENTS.md §Style rules):\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_snake_case_python_files(src_root: Path) -> None:
    """All module filenames must be snake_case (or ``__dunder__`` files)."""
    # Allow: dunder (``__init__``), private (``_sources``), and standard snake_case.
    valid_pattern = re.compile(r"^(__[a-z_]+__|_?[a-z][a-z0-9_]*)\.py$")
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        if not valid_pattern.match(py.name):
            offenders.append(str(py.relative_to(src_root)))
    assert not offenders, "Non snake_case Python filenames (AGENTS.md §Style rules):\n" + "\n".join(
        offenders
    )
