"""Spark-free table introspection for folder-shaped stores.

Folder stores (disc, s3, azure_blob, gcs) hold one *data file* per
table — or a Spark-written ``<table>.csv/`` / ``<table>.parquet/``
directory of part files. The Data Space explorer needs the table's
fields, a row count, a sample and an export stream **without** a Spark
session (it runs in the short-lived ``ovid2 tools describe-table`` /
``sample-table`` / ``count-rows`` / ``export-table`` executor
subprocess). This module provides that on top of a tiny storage
abstraction each driver implements:

- :class:`TabularFile` — one physical file (name, size, mtime) that can
  be opened as a *seekable* binary stream.
- :class:`TabularSource` — ``stat(path)`` / ``children(path)`` /
  ``open(file)`` for a store.

:class:`RangeReader` turns any "read bytes ``[start, end)``" callable
(S3 ranged ``GetObject``, Azure ``download_blob(offset, length)``, GCS
``download_as_bytes(start, end)``) into a seekable file object, so
pyarrow can read parquet footers remotely without downloading the
whole object.

Formats: delimited text (``.csv`` / ``.tsv`` / ``.txt``, optionally
``.gz``), parquet, line-delimited JSON. Anything else is described by
its stats only (``previewable=False``).

Import-safe: stdlib at module scope; ``pyarrow`` is imported lazily
inside the parquet helpers.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any, Protocol

from ovid2.drivers._table_listing import is_listing_junk
from ovid2.drivers.base import ColumnInfo, TableInfo, TableSample

__all__ = [
    "MAX_SAMPLE_ROWS",
    "ObjectStoreSource",
    "RangeReader",
    "TabularFile",
    "TabularIntrospectionMixin",
    "TabularSource",
    "count_table_rows",
    "describe_table",
    "detect_format",
    "iso_utc",
    "iter_table_rows",
    "sample_table",
]

#: Hard ceiling on a sample request (the explorer offers 25 / 100 / 500).
MAX_SAMPLE_ROWS = 1000

#: Delimiters tried (in order of preference on a tie).
_DELIMITER_CANDIDATES: tuple[str, ...] = (",", "|", "\t", ";")

#: Rows read to infer text column types / estimate a row count.
_INFER_ROWS = 200

#: Bytes read for a row-count *estimate* on large uncompressed text.
_ESTIMATE_BYTES = 1 << 20

#: Raise the csv field-size ceiling (clinical notes fields are large).
csv.field_size_limit(1 << 28)


# ---------------------------------------------------------------------------
# Storage abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TabularFile:
    """One physical file of a table.

    Attributes:
        path: Store-specific path (relative to the storage-account root).
        name: Leaf name.
        size_bytes: Size when known.
        modified_at: ISO-8601 UTC mtime when known.
        is_dir: ``True`` for a directory (part-file container).
    """

    path: str
    name: str
    size_bytes: int | None = None
    modified_at: str | None = None
    is_dir: bool = False


class TabularSource(Protocol):
    """What a folder-store driver must expose for table introspection."""

    def stat(self, path: str) -> TabularFile:
        """Return the file / directory at ``path`` (``FileNotFoundError`` if absent)."""
        ...

    def children(self, path: str) -> list[TabularFile]:
        """Return the direct children of directory ``path``."""
        ...

    def open(self, file: TabularFile) -> IO[bytes]:
        """Open ``file`` as a seekable binary stream."""
        ...


def iso_utc(value: float | datetime | None) -> str | None:
    """Render an epoch / datetime as an ISO-8601 UTC string (``None`` passes)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        dt = datetime.fromtimestamp(float(value), tz=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RangeReader(io.RawIOBase):
    """A seekable, read-only file object over a ranged-read callable.

    Args:
        size: Total object size in bytes.
        fetch: ``fetch(start, end_exclusive) -> bytes``.
    """

    def __init__(
        self,
        size: int,
        fetch: Callable[[int, int], bytes],
        *,
        read_ahead: int = 4 << 20,
    ) -> None:
        super().__init__()
        self._size = int(size)
        self._fetch = fetch
        self._pos = 0
        self._read_ahead = max(0, int(read_ahead))
        self._buf_start = 0
        self._buf = b""

    def readable(self) -> bool:
        """Return ``True`` — always readable."""
        return True

    def seekable(self) -> bool:
        """Return ``True`` — ranged reads support random access."""
        return True

    def tell(self) -> int:
        """Return the current offset."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the cursor (standard ``whence`` semantics)."""
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        self._pos = max(0, pos)
        return self._pos

    def readinto(self, buffer: Any) -> int:
        """Fill ``buffer`` from the current offset; return bytes read."""
        view = memoryview(buffer).cast("B")
        if self._pos >= self._size or not len(view):
            return 0
        want = min(len(view), self._size - self._pos)
        buf_end = self._buf_start + len(self._buf)
        if not (self._buf_start <= self._pos and self._pos + want <= buf_end):
            end = min(self._size, self._pos + max(want, self._read_ahead))
            self._buf = self._fetch(self._pos, end)
            self._buf_start = self._pos
        offset = self._pos - self._buf_start
        data = self._buf[offset : offset + want]
        n = len(data)
        view[:n] = data
        self._pos += n
        return n

    @property
    def size(self) -> int:
        """Total object size."""
        return self._size


class ObjectStoreSource:
    """A :class:`TabularSource` over a flat object namespace (S3 / Azure / GCS).

    Paths are keys relative to ``base`` (the address's ``location.path``,
    usually empty when browsing a whole storage account). "Directories"
    are key prefixes.

    Args:
        head: ``head(key) -> (size, modified_at_iso) | None`` (``None`` =
            no object at exactly ``key``).
        listing: ``listing(prefix) -> (dir_names, [(name, size, mtime)])``
            one level under ``prefix`` (``prefix`` ends with ``/`` or is
            empty).
        fetch: ``fetch(key, start, end_exclusive) -> bytes``.
        base: Key prefix every path is relative to.
    """

    def __init__(
        self,
        *,
        head: Callable[[str], tuple[int, str | None] | None],
        listing: Callable[[str], tuple[list[str], list[tuple[str, int, str | None]]]],
        fetch: Callable[[str, int, int], bytes],
        base: str = "",
    ) -> None:
        self._head = head
        self._listing = listing
        self._fetch = fetch
        self._base = base.strip().strip("/")

    def _key(self, path: str) -> str:
        rel = (path or "").strip().strip("/")
        if any(part in ("..", ".") for part in rel.split("/")):
            raise ValueError(f"path {path!r} may not contain '.' / '..' segments.")
        return "/".join(p for p in (self._base, rel) if p)

    def _rel(self, key: str) -> str:
        return key[len(self._base) + 1 :] if self._base else key

    def stat(self, path: str) -> TabularFile:
        """Return the object / prefix at ``path``."""
        key = self._key(path)
        name = key.rsplit("/", 1)[-1]
        found = self._head(key) if key else None
        if found is not None:
            size, mtime = found
            return TabularFile(path=self._rel(key), name=name, size_bytes=size, modified_at=mtime)
        dirs, files = self._listing(key + "/" if key else "")
        if not dirs and not files:
            raise FileNotFoundError(f"'{path}' does not exist.")
        mtime = max((f[2] for f in files if f[2]), default=None)
        return TabularFile(path=self._rel(key), name=name, modified_at=mtime, is_dir=True)

    def children(self, path: str) -> list[TabularFile]:
        """Return the objects / sub-prefixes one level under ``path``."""
        key = self._key(path)
        prefix = key + "/" if key else ""
        dirs, files = self._listing(prefix)
        out = [TabularFile(path=self._rel(prefix + d), name=d, is_dir=True) for d in dirs]
        out.extend(
            TabularFile(path=self._rel(prefix + n), name=n, size_bytes=s, modified_at=m)
            for n, s, m in files
        )
        return out

    def open(self, file: TabularFile) -> IO[bytes]:
        """Open the object as a seekable ranged reader."""
        key = self._key(file.path)
        size = file.size_bytes
        if size is None:
            found = self._head(key)
            if found is None:
                raise FileNotFoundError(f"'{file.path}' does not exist.")
            size = found[0]
        reader = RangeReader(size, lambda s, e: self._fetch(key, s, e))
        return io.BufferedReader(reader, buffer_size=1 << 20)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_TEXT_EXT = re.compile(r"\.(csv|tsv|txt|dat|psv)(\.gz)?$", re.IGNORECASE)
_PARQUET_EXT = re.compile(r"\.parquet$", re.IGNORECASE)
_JSON_EXT = re.compile(r"\.(json|jsonl|ndjson)(\.gz)?$", re.IGNORECASE)


def detect_format(name: str) -> str:
    """Return ``csv`` | ``parquet`` | ``json`` | ``unknown`` from a file / dir name."""
    lowered = name.rstrip("/").lower()
    if _PARQUET_EXT.search(lowered):
        return "parquet"
    if _JSON_EXT.search(lowered):
        return "json"
    if _TEXT_EXT.search(lowered):
        return "csv"
    return "unknown"


def _part_files(source: TabularSource, root: TabularFile) -> list[TabularFile]:
    """Return the data part files of a directory table (sorted, junk dropped)."""
    parts = [
        c
        for c in source.children(root.path)
        if not c.is_dir and not is_listing_junk(c.name) and (c.size_bytes or 0) > 0
    ]
    return sorted(parts, key=lambda c: c.name)


def _resolve(source: TabularSource, path: str) -> tuple[TabularFile, list[TabularFile], str]:
    """Return ``(root, part_files, format)`` for the table at ``path``."""
    root = source.stat(path)
    if root.is_dir:
        parts = _part_files(source, root)
        fmt = detect_format(root.name)
        if fmt == "unknown" and parts:
            fmt = detect_format(parts[0].name)
        return root, parts, fmt
    return root, [root], detect_format(root.name)


# ---------------------------------------------------------------------------
# Text (delimited) helpers
# ---------------------------------------------------------------------------


def _text_stream(source: TabularSource, file: TabularFile) -> IO[str]:
    """Open ``file`` as a decoded text stream (transparent gunzip)."""
    raw: Any = source.open(file)
    buffered: Any = raw if isinstance(raw, io.BufferedIOBase) else io.BufferedReader(raw, 1 << 20)
    if file.name.lower().endswith(".gz"):
        buffered = gzip.GzipFile(fileobj=buffered, mode="rb")
    return io.TextIOWrapper(buffered, encoding="utf-8", errors="replace", newline="")


def _detect_delimiter(first_line: str, name: str) -> str:
    """Pick the delimiter that occurs most often in the header line."""
    if name.lower().rstrip("/").endswith((".tsv", ".tsv.gz")):
        return "\t"
    counts = {d: first_line.count(d) for d in _DELIMITER_CANDIDATES}  # noqa: OVID2001
    best = max(_DELIMITER_CANDIDATES, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _text_header(source: TabularSource, file: TabularFile) -> tuple[list[str], str]:
    """Return ``(columns, delimiter)`` from the first line of a text file."""
    with _text_stream(source, file) as fh:
        first = fh.readline()
    first = first.lstrip("\ufeff")
    delimiter = _detect_delimiter(first, file.name)
    header = next(csv.reader([first.rstrip("\r\n")], delimiter=delimiter), [])
    return [h.strip() for h in header], delimiter


_INT_RE = re.compile(r"^[+-]?\d+$")
_DEC_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?")


def _infer_type(values: list[str]) -> str:
    """Infer a display type from sample string values."""
    present = [v.strip() for v in values if v is not None and v.strip() != ""]
    if not present:
        return "string"
    for kind, pattern in (
        ("integer", _INT_RE),
        ("decimal", _DEC_RE),
        ("date", _DATE_RE),
        ("timestamp", _TS_RE),
    ):
        if all(pattern.match(v) for v in present):
            return kind
    return "string"


def _iter_text_rows(
    source: TabularSource, parts: list[TabularFile], delimiter: str
) -> Iterator[list[str]]:
    """Yield data rows across part files, skipping each part's header line."""
    for part in parts:
        with _text_stream(source, part) as fh:
            reader = csv.reader(fh, delimiter=delimiter)
            first = True
            for row in reader:
                if first:
                    first = False
                    continue
                if not row or (len(row) == 1 and row[0] == ""):
                    continue
                yield row


def _estimate_text_rows(source: TabularSource, parts: list[TabularFile]) -> int | None:
    """Estimate the row count of uncompressed text parts from the first MiB."""
    total_size = sum(p.size_bytes or 0 for p in parts)
    if not parts or not total_size or any(p.name.lower().endswith(".gz") for p in parts):
        return None
    with source.open(parts[0]) as raw:
        chunk = raw.read(_ESTIMATE_BYTES)
    if not chunk:
        return 0
    lines = chunk.count(b"\n")  # noqa: OVID2001 — bytes.count, not a Spark action
    if len(chunk) < _ESTIMATE_BYTES and len(parts) == 1:
        # Whole file read: exact (minus the header line).
        trailing = 0 if chunk.endswith(b"\n") else 1
        return max(0, lines + trailing - 1)
    if lines <= 1:
        return None
    per_line = len(chunk) / lines
    return max(0, int(total_size / per_line) - len(parts))


# ---------------------------------------------------------------------------
# Parquet helpers (pyarrow, lazy)
# ---------------------------------------------------------------------------


def _parquet_file(source: TabularSource, file: TabularFile) -> Any:
    """Return a ``pyarrow.parquet.ParquetFile`` over ``file``."""
    import pyarrow.parquet as pq

    return pq.ParquetFile(source.open(file))


def _arrow_type_name(field: Any) -> str:
    """Return a readable Arrow type string."""
    return str(field.type)


# ---------------------------------------------------------------------------
# JSON-lines helpers
# ---------------------------------------------------------------------------


def _iter_json_rows(source: TabularSource, parts: list[TabularFile]) -> Iterator[dict[str, Any]]:
    """Yield one dict per JSON line across part files."""
    for part in parts:
        with _text_stream(source, part) as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def describe_table(source: TabularSource, path: str) -> TableInfo:
    """Return the :class:`TableInfo` of the file / directory table at ``path``.

    Cheap: header line, parquet footers, first MiB for a row estimate.
    """
    root, parts, fmt = _resolve(source, path)
    size = sum(p.size_bytes or 0 for p in parts) if root.is_dir else root.size_bytes
    modified = max((p.modified_at for p in parts if p.modified_at), default=root.modified_at)
    base: dict[str, Any] = {
        "name": root.name.rstrip("/"),
        "path": path,
        "kind": "file",
        "format": fmt,
        "size_bytes": size,
        "modified_at": modified,
        "parts": len(parts) if root.is_dir else None,
    }
    if root.is_dir and not parts:
        return TableInfo(**base, previewable=False, message="The directory holds no data files.")
    if fmt == "parquet":
        columns: tuple[ColumnInfo, ...] = ()
        total = 0
        for i, part in enumerate(parts):
            pf = _parquet_file(source, part)
            if i == 0:
                columns = tuple(
                    ColumnInfo(name=f.name, type=_arrow_type_name(f), nullable=bool(f.nullable))
                    for f in pf.schema_arrow
                )
            total += int(pf.metadata.num_rows)
        return TableInfo(**base, columns=columns, row_count=total, row_count_exact=True)
    if fmt == "csv":
        header, delimiter = _text_header(source, parts[0])
        samples: list[list[str]] = []
        for row in _iter_text_rows(source, parts[:1], delimiter):
            samples.append(row)
            if len(samples) >= _INFER_ROWS:
                break
        columns = tuple(
            ColumnInfo(
                name=name or f"column_{i + 1}",
                type=_infer_type([r[i] if i < len(r) else "" for r in samples]),
            )
            for i, name in enumerate(header)
        )
        estimate = _estimate_text_rows(source, parts)
        exact = (
            estimate is not None
            and len(parts) == 1
            and ((parts[0].size_bytes or 0) < _ESTIMATE_BYTES)
        )
        return TableInfo(
            **base,
            columns=columns,
            row_count=estimate,
            row_count_exact=exact,
            delimiter=delimiter,
            message=None if exact else "Row count is an estimate; use Count rows for an exact one.",
        )
    if fmt == "json":
        keys: list[str] = []
        seen: set[str] = set()
        for i, obj in enumerate(_iter_json_rows(source, parts[:1])):
            for k in obj:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
            if i >= _INFER_ROWS:
                break
        return TableInfo(
            **base,
            columns=tuple(ColumnInfo(name=k, type="json") for k in keys),
            row_count=None,
            row_count_exact=False,
        )
    return TableInfo(
        **base,
        previewable=False,
        message="Not a recognised data file (csv / tsv / txt / parquet / json); stats only.",
    )


def iter_table_rows(
    source: TabularSource,
    path: str,
    limit: int | None = None,
    *,
    batch_size: int = 5000,
) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
    """Stream the rows of the table at ``path`` as ``(columns, batches)``.

    Raises:
        ValueError: When the file is not a recognised data file.
    """
    root, parts, fmt = _resolve(source, path)
    del root
    if not parts:
        return [], iter(())
    batch_size = max(1, int(batch_size))

    if fmt == "parquet":
        first = _parquet_file(source, parts[0])
        columns = [f.name for f in first.schema_arrow]

        def _parquet_batches() -> Iterator[list[tuple[Any, ...]]]:
            remaining = limit
            for part in parts:
                pf = _parquet_file(source, part)
                for record_batch in pf.iter_batches(batch_size=batch_size):
                    rows = list(
                        zip(*(col.to_pylist() for col in record_batch.columns), strict=False)
                    )
                    if remaining is not None:
                        rows = rows[:remaining]
                        remaining -= len(rows)
                    if rows:
                        yield rows
                    if remaining is not None and remaining <= 0:
                        return

        return columns, _parquet_batches()

    if fmt == "csv":
        header, delimiter = _text_header(source, parts[0])
        columns = [h or f"column_{i + 1}" for i, h in enumerate(header)]
        width = len(columns)

        def _text_batches() -> Iterator[list[tuple[Any, ...]]]:
            batch: list[tuple[Any, ...]] = []
            for n, row in enumerate(_iter_text_rows(source, parts, delimiter)):
                if limit is not None and n >= limit:
                    break
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                batch.append(tuple(v if v != "" else None for v in row[:width]))
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch

        return columns, _text_batches()

    if fmt == "json":
        cols: list[str] = []
        seen: set[str] = set()
        for i, row in enumerate(_iter_json_rows(source, parts[:1])):
            for k in row:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
            if i >= _INFER_ROWS:
                break

        def _json_batches() -> Iterator[list[tuple[Any, ...]]]:
            batch: list[tuple[Any, ...]] = []
            for n, row in enumerate(_iter_json_rows(source, parts)):
                if limit is not None and n >= limit:
                    break
                batch.append(
                    tuple(
                        v if not isinstance(v, (dict, list)) else json.dumps(v)
                        for v in (row.get(c) for c in cols)
                    )
                )
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch

        return cols, _json_batches()

    raise ValueError(f"'{path}' is not a recognised data file (csv / parquet / json).")


def sample_table(source: TabularSource, path: str, limit: int = 100) -> TableSample:
    """Return the first ``limit`` rows (capped at :data:`MAX_SAMPLE_ROWS`)."""
    limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    columns, batches = iter_table_rows(source, path, limit + 1, batch_size=limit + 1)
    rows: list[tuple[Any, ...]] = []
    for batch in batches:
        rows.extend(batch)
    truncated = len(rows) > limit
    return TableSample(columns=tuple(columns), rows=tuple(rows[:limit]), truncated=truncated)


def count_table_rows(source: TabularSource, path: str) -> int:
    """Return the exact row count (parquet footers; full scan for text / JSON)."""
    _root, parts, fmt = _resolve(source, path)
    if fmt == "parquet":
        return sum(int(_parquet_file(source, p).metadata.num_rows) for p in parts)
    if fmt == "csv":
        _header, delimiter = _text_header(source, parts[0])
        return sum(1 for _ in _iter_text_rows(source, parts, delimiter))
    if fmt == "json":
        return sum(1 for _ in _iter_json_rows(source, parts))
    raise ValueError(f"'{path}' is not a recognised data file (csv / parquet / json).")


class TabularIntrospectionMixin:
    """Implements the four :class:`~ovid2.drivers.base.Driver` introspection hooks.

    A folder-store driver mixes this in (before :class:`Driver`) and
    supplies :meth:`_tabular_source`; ``describe_table`` /
    ``sample_rows`` / ``iter_rows`` / ``count_rows`` follow.
    """

    def _tabular_source(self) -> TabularSource:  # pragma: no cover - abstract
        """Return the store's :class:`TabularSource`."""
        raise NotImplementedError

    def describe_table(self, path: str) -> TableInfo:
        """Describe the data file / part-directory at listing ``path``."""
        return describe_table(self._tabular_source(), path)

    def sample_rows(self, path: str, limit: int = 100) -> TableSample:
        """Return the first ``limit`` rows of the data file at ``path``."""
        return sample_table(self._tabular_source(), path, limit)

    def iter_rows(
        self, path: str, limit: int | None = None, *, batch_size: int = 5000
    ) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
        """Stream the rows of the data file at ``path``."""
        return iter_table_rows(self._tabular_source(), path, limit, batch_size=batch_size)

    def count_rows(self, path: str) -> int:
        """Exact row count (parquet footers / full text scan)."""
        return count_table_rows(self._tabular_source(), path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
