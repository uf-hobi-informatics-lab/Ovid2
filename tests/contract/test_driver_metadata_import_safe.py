"""Contract: driver metadata modules stay import-safe.

Every driver folder under ``src/ovid2/drivers/<type>/`` may split its
implementation across up to four files:

- ``metadata.py`` — pure data (:class:`~ovid2.drivers.base.ConnectionFieldSpec`,
  container requirements). Imported by the web tier + MCP + CLI.
- ``driver.py`` — the :class:`~ovid2.drivers.base.Driver` subclass.
- ``probe.py`` — optional probe implementations (may import SDKs
  lazily).
- ``spark.py`` — the *only* file per driver that may
  ``import pyspark``.

This test enforces that ``metadata.py`` never imports heavy
peripheral SDKs (pyspark, boto3, azure-*, snowflake-*,
databricks-*, google-cloud-*) — those must always be gated to
``driver.py`` / ``probe.py`` / ``spark.py`` where the imports live
either inside method bodies or under a ``TYPE_CHECKING`` guard.

The check is grep-based (mirrors the style of
``test_no_pyspark_outside_engine.py``); an ``import pyspark`` inside
a method body is fine because it never runs at import time.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVERS_DIR = REPO_ROOT / "src" / "ovid2" / "drivers"

#: Substrings that indicate a top-level import of a heavy peripheral SDK.
#: A driver metadata module may reference these names inside a docstring or
#: string literal, but must never import them at module scope.
_HEAVY_IMPORT_LINE = re.compile(
    r"^(?:from|import)\s+(?:"
    r"pyspark"
    r"|boto3"
    r"|botocore"
    r"|azure\.storage\.blob"
    r"|azure\.identity"
    r"|snowflake\.connector"
    r"|snowflake\.snowpark"
    r"|databricks\.connect"
    r"|google\.cloud"
    r")\b",
)


def _iter_metadata_files() -> list[Path]:
    """Return every ``drivers/*/metadata.py`` under ``src/ovid2/drivers``."""
    if not DRIVERS_DIR.is_dir():
        return []
    return sorted(DRIVERS_DIR.glob("*/metadata.py"))


def _is_top_level_import(line: str) -> bool:
    """Return True when ``line`` is an unindented ``import`` / ``from``."""
    stripped = line.lstrip()
    if not stripped.startswith(("import ", "from ")):
        return False
    return line == stripped  # zero leading whitespace


def test_driver_metadata_modules_have_no_heavy_imports() -> None:
    """No ``metadata.py`` may import a heavy peripheral SDK at module scope.

    Method-body imports (indented) and ``TYPE_CHECKING`` guards are
    fine; the point is that ``importlib.import_module(
    'ovid2.drivers.s3.metadata')`` must succeed on a host without
    boto3 installed.
    """
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_metadata_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if not _is_top_level_import(line):
                continue
            if _HEAVY_IMPORT_LINE.match(line.strip()):
                offenders.append((path, lineno, line.rstrip()))

    assert not offenders, (
        "Driver metadata modules must be import-safe (no top-level "
        "imports of pyspark / boto3 / azure-* / snowflake-* / databricks-* "
        "/ google-cloud-*). Offending imports:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}:{n}: {ln}" for p, n, ln in offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
