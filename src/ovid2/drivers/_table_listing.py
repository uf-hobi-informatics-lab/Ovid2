"""Shared helpers for turning a storage listing into copy/move table specs.

Folder-shaped sources (disc, s3, azure_blob) hold one file (or one
Spark-written ``<table>.csv/`` directory) per table under
``{root}/{pipeline}/{batch_id}/``. These helpers convert a
:class:`~ovid2.drivers.base.StorageEntry` listing of that folder into
the ``LOGICAL=SOURCE`` specs ``ovid2 tools copy --tables`` accepts
(:mod:`ovid2.tools.table_specs`), and look up per-table sizes for the
largest-first copy scheduler.

Import-safe: stdlib only, so the web tier and every driver module can
use it without pulling an SDK.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ovid2.drivers.base import StorageEntry

#: Extensions stripped (longest first) to derive the logical table name.
_TABLE_EXTENSIONS: tuple[str, ...] = (
    ".csv.gz",
    ".txt.gz",
    ".tsv.gz",
    ".snappy.parquet",
    ".parquet",
    ".csv",
    ".txt",
    ".tsv",
    ".json",
    ".jsonl",
    ".gz",
)


def strip_table_extension(name: str) -> str:
    """Return ``name`` without a known data-file extension (case-insensitive)."""
    lower = name.lower()
    for ext in _TABLE_EXTENSIONS:
        if lower.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)]
    return name


def is_listing_junk(name: str) -> bool:
    """Return ``True`` for entries that are never a table.

    Hidden files, Spark / Hadoop markers (``_SUCCESS``, ``_metadata``,
    ``.crc``), Hive partition directories (``k=v`` — ``=`` is also the
    table-spec separator), and the copy tool's own staging leftovers.
    """
    if not name or name.startswith((".", "_")):
        return True
    if "=" in name:
        return True
    return name.lower().endswith((".crc", ".tmp", ".lock"))


def entries_to_table_specs(entries: Iterable[StorageEntry]) -> list[str]:
    """Convert a batch-folder listing to ``--tables`` specs.

    Each non-junk ``file`` / ``table`` entry becomes either a bare name
    (when no extension is stripped) or ``stem=name`` so the destination
    stem is extension-free while the source lookup stays exact. A
    ``dir`` entry counts as a table only when it carries a data
    extension (the Spark-written ``demographic.csv/`` shape) — plain
    sub-folders (other batches, ``logs/``) are skipped. Duplicate stems
    (``x.csv`` + ``x.parquet``) keep the first entry in listing order.
    """
    specs: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.name.rstrip("/")
        if is_listing_junk(name):
            continue
        stem = strip_table_extension(name)
        if entry.kind == "dir" and stem == name:
            continue
        key = stem.lower()
        if key in seen:
            continue
        seen.add(key)
        specs.append(stem if stem == name else f"{stem}={name}")
    return specs


def sizes_for(
    entries: Iterable[StorageEntry],
    file_names: Iterable[str],
) -> dict[str, int]:
    """Map each ``file_name`` (exact or glob) to the summed size of its matches.

    Only entries whose ``size_bytes`` is known contribute; a file name
    with no sized match is omitted (== unknown). Matching is
    case-insensitive, mirroring the disc resolver's tolerance.
    """
    sized = [
        (e.name.rstrip("/").lower(), int(e.size_bytes)) for e in entries if e.size_bytes is not None
    ]
    out: dict[str, int] = {}
    for file_name in file_names:
        pattern = file_name.lower()
        total = 0
        matched = False
        for name, size in sized:
            if name == pattern or fnmatch.fnmatchcase(name, pattern):
                total += size
                matched = True
        if matched:
            out[file_name] = total
    return out


class PrefixTableListingMixin:
    """``all`` expansion + size hints for prefix-keyed object stores.

    For drivers whose read path is ``{location.path}/{pipeline}/{batch}/{file}``
    with no layout fallback (s3, azure_blob). The concrete driver
    supplies :meth:`_list_tables` (a recursive, size-aggregating listing
    under a sub-prefix); this mixin turns it into the
    :meth:`Driver.expand_all_tables` / :meth:`Driver.table_sizes`
    contract.
    """

    def _list_tables(self, sub_prefix: str) -> list[StorageEntry]:  # pragma: no cover
        """Return size-aggregated children of ``sub_prefix`` (driver-specific)."""
        raise NotImplementedError

    @staticmethod
    def _batch_prefix(pipeline: str, batch_id: str) -> str:
        """Return ``pipeline/batch_id`` with empty segments dropped (matches ``read``)."""
        return "/".join(p for p in (pipeline, batch_id) if p)

    @classmethod
    def supports_expand_all(cls) -> bool:
        """A batch prefix is enumerable, so ``all`` is expandable."""
        return True

    def expand_all_tables(
        self,
        location: dict[str, object] | None = None,
        *,
        pipeline: str | None = None,
        batch_id: str | None = None,
    ) -> list[str] | None:
        """Expand ``all`` to every table object under the batch prefix.

        Raises:
            FileNotFoundError: When the prefix holds no objects.
        """
        del location  # the driver's own location is what ``read`` uses
        if not pipeline or not batch_id:
            return None
        sub = self._batch_prefix(pipeline, batch_id)
        entries = self._list_tables(sub)
        if not entries:
            raise FileNotFoundError(f"no objects under prefix {sub!r}")
        return entries_to_table_specs(entries)

    def table_sizes(
        self,
        file_names: list[str],
        *,
        pipeline: str,
        batch_id: str,
    ) -> dict[str, int]:
        """Return summed object sizes for ``file_names`` under the batch prefix."""
        return sizes_for(self._list_tables(self._batch_prefix(pipeline, batch_id)), file_names)


__all__ = [
    "PrefixTableListingMixin",
    "entries_to_table_specs",
    "is_listing_junk",
    "sizes_for",
    "strip_table_extension",
]
