"""Contract: an address is the *exact* location of its data.

Ovid2 is a processor — a command plus an input address and an output
address. Every address (disc folder, object-store prefix, warehouse
``DATABASE/SCHEMA``) is the exact location the data is read from or
written to; neither ``pipeline`` nor ``batch_id`` is ever appended as a
path segment (ADR:
the adr-address-is-exact-location design note).

This test walks the driver data-plane modules and fails if any of them
re-introduces the pre-2026-10 layouts:

- ``{root}/{pipeline}/{batch_id}`` — joining ``pipeline`` and
  ``batch_id`` into a persistent read / write path;
- ``OVID_{pipeline}_{batch_id}`` — the synthetic Snowflake schema.

Transient staging names (instance-scoped ``@~`` stage dirs, temporary
stage / table identifiers, the Databricks bounce sub-prefix) are
allow-listed: they are scratch space owned by one run, never the
operator-visible location.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Patterns that would mean a driver builds a path out of pipeline + batch.
_FORBIDDEN: tuple[re.Pattern[str], ...] = (
    re.compile(r"\(\s*(?:self\.)?(?:prefix|root|root_path|base)\s*,\s*pipeline\s*,\s*batch_id"),
    re.compile(r"os\.path\.join\([^)]*pipeline[^)]*batch_id"),
    re.compile(r"/\s*pipeline\s*/\s*batch_id"),
    re.compile(r"OVID_\{pipeline\}"),
)

#: ``(relative file, line fragment)`` pairs that are transient scratch names.
_ALLOWED_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("drivers/snowflake/staging/disc.py", "SNOWPARK_STAGING_INGEST_PREFIX}/{instance_id}"),
    ("drivers/snowflake/staging/disc.py", "SNOWPARK_STAGING_EGEST_PREFIX}/{instance_id}"),
    ("drivers/snowflake/staging/databricks.py", 'sub = f"{instance_id}/{pipeline}/{batch_id}'),
)


def _data_plane_files(src_root: Path) -> list[Path]:
    """Return every driver / stager module that resolves storage paths."""
    files = sorted((src_root / "drivers").rglob("*.py"))
    files.append(src_root / "core" / "engines" / "stager.py")
    files.append(src_root / "cli" / "tools.py")
    return [f for f in files if "__pycache__" not in f.parts]


@pytest.mark.contract
def test_no_pipeline_batch_path_segments(src_root: Path) -> None:
    """No driver composes ``pipeline`` + ``batch_id`` into a storage path."""
    offenders: list[str] = []
    for path in _data_plane_files(src_root):
        rel = path.relative_to(src_root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not any(p.search(line) for p in _FORBIDDEN):
                continue
            if any(rel == f and frag in line for f, frag in _ALLOWED_FRAGMENTS):
                continue
            offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "Drivers must treat the address as the exact location — no "
        "{pipeline}/{batch_id} segments or OVID_{pipeline}_{batch} schemas "
        "(see adr-address-is-exact-location.md):\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_snowflake_has_no_synthetic_schema() -> None:
    """``SnowflakeAddress`` must not expose the removed ``_schema_name``."""
    from ovid2.drivers.snowflake.spark import SnowflakeAddress

    assert not hasattr(SnowflakeAddress, "_schema_name")


@pytest.mark.contract
def test_disc_resolver_signature_is_exact() -> None:
    """``resolve_read_path`` takes ``(root, pattern)`` only — no pipeline/batch."""
    import inspect

    from ovid2.drivers.disc.spark import resolve_read_path

    assert list(inspect.signature(resolve_read_path).parameters) == ["root_path", "file_pattern"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
