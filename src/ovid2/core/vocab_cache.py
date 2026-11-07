"""Columnar cache for large file-sourced shared views (OMOP vocabulary).

The OMOP ``CONCEPT.csv`` (~1.3 GB) and ``CONCEPT_RELATIONSHIP.csv``
(~2.7 GB) are registered as shared views for every run. Re-parsing the
tab-separated text for every table that resolves concepts dominated
the PySpark wall-clock on OMOP routes. This module keys a one-time
Parquet conversion of such a file by ``(name, size, mtime, delimiter,
header)`` so every later run (and every engine — Spark and DuckDB both
read the same Parquet directory) scans compressed columns instead.

Layout::

    <cache_root>/<stem>-<size>-<mtime_ns>-<delim_hex>-<h|n>.parquet/
        part-*.parquet      (Spark)  or  data_*.parquet (DuckDB)
        _OVID2_COMPLETE     (marker written last)

The marker makes a half-written directory (crash mid-build) invisible;
builders write into a unique temp sibling and ``os.rename`` it into
place, so concurrent runs never observe partial output.

Pure path logic: no engine import. The actual conversion is done by the
engine-specific caller (``core/pipeline.py`` for Spark, the DuckDB
stager for DuckDB).

Settings:

* ``OVID2_VOCAB_PARQUET_CACHE`` — ``0`` disables the cache (default on).
* ``OVID2_VOCAB_CACHE_DIR`` — cache root. Default: a ``.ovid2_parquet``
  directory next to the source file when that directory is writable.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

COMPLETE_MARKER: str = "_OVID2_COMPLETE"
_DEFAULT_SUBDIR: str = ".ovid2_parquet"


def cache_enabled() -> bool:
    """Return ``False`` when ``$OVID2_VOCAB_PARQUET_CACHE`` disables the cache."""
    raw = os.environ.get("OVID2_VOCAB_PARQUET_CACHE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def cache_root_for(source: Path) -> Path | None:
    """Return the cache root for ``source`` or ``None`` if none is writable."""
    override = os.environ.get("OVID2_VOCAB_CACHE_DIR", "").strip()
    root = Path(override) if override else source.parent / _DEFAULT_SUBDIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root if os.access(root, os.W_OK | os.X_OK) else None


def cache_key(source: Path, *, delimiter: str, header: bool) -> str:
    """Return the cache directory name for ``source`` + its CSV options."""
    stat = source.stat()
    delim_hex = delimiter.encode("utf-8").hex() or "00"
    return f"{source.stem}-{stat.st_size}-{stat.st_mtime_ns}-{delim_hex}-{'h' if header else 'n'}.parquet"


def cached_parquet(source: Path, *, delimiter: str, header: bool) -> Path | None:
    """Return the completed Parquet directory for ``source`` if one exists."""
    if not cache_enabled():
        return None
    root = cache_root_for(source)
    if root is None:
        return None
    target = root / cache_key(source, delimiter=delimiter, header=header)
    return target if (target / COMPLETE_MARKER).is_file() else None


def build_target(source: Path, *, delimiter: str, header: bool) -> tuple[Path, Path] | None:
    """Return ``(temp_dir, final_dir)`` for a new build, or ``None``.

    ``None`` when the cache is disabled or no writable root exists — the
    caller then reads the CSV directly.
    """
    if not cache_enabled():
        return None
    root = cache_root_for(source)
    if root is None:
        return None
    final = root / cache_key(source, delimiter=delimiter, header=header)
    temp = root / f".building-{final.name}-{uuid.uuid4().hex[:8]}"
    return temp, final


def publish(temp: Path, final: Path) -> Path:
    """Mark ``temp`` complete and atomically move it to ``final``.

    If another run won the race, the loser's ``temp`` is discarded and
    the winner's directory is returned.
    """
    (temp / COMPLETE_MARKER).write_text("ok\n", encoding="ascii")
    try:
        os.rename(temp, final)
    except OSError:
        shutil.rmtree(temp, ignore_errors=True)
        if not (final / COMPLETE_MARKER).is_file():
            raise
    _prune_stale(final)
    return final


def discard(temp: Path) -> None:
    """Remove a failed build's temp directory (best-effort)."""
    shutil.rmtree(temp, ignore_errors=True)


def _prune_stale(final: Path) -> None:
    """Remove older cache entries for the same source stem (best-effort)."""
    # Key = ``<stem>-<size>-<mtime>-<delim>-<h|n>.parquet``: strip the
    # four trailing fields to recover the (possibly dashed) stem.
    stem = final.name.rsplit("-", 4)[0]
    for sibling in final.parent.glob(f"{stem}-*.parquet"):
        if sibling == final or sibling.name.rsplit("-", 4)[0] != stem:
            continue
        shutil.rmtree(sibling, ignore_errors=True)
        logger.info("Pruned stale vocabulary cache %s", sibling)


__all__ = [
    "COMPLETE_MARKER",
    "build_target",
    "cache_enabled",
    "cache_key",
    "cache_root_for",
    "cached_parquet",
    "discard",
    "publish",
]
