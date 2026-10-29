"""Data Space table detail / sample / count / export service (web tier).

The explorer's table detail page needs live data access (fields, a row
sample, a row count, an export). Every one of those is credential-
bearing, so — exactly like :mod:`storage_browse_svc` — this module
never opens a store itself: it validates the Data Space path, spawns
the matching ``ovid2 tools describe-table | sample-table | count-rows
| export-table`` executor subprocess through
:func:`~ovid2.web.services._broker.run_tool_json`, and maps the JSON
result onto a typed model. No ``resolve_secrets`` / driver import here
(``tests/contract/test_web_no_plaintext_secret_reads.py``) and no
document-library import (``tests/contract/test_web_no_export_imports.py``).

Export files are written by the executor into
``$XDG_STATE_HOME/ovid2/web_exports/<token>/<file>`` (mode 0600) and
served once by the API; :func:`prune_exports` drops anything older than
:data:`EXPORT_TTL_SECONDS`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ovid2 import dataspace
from ovid2.config import paths as xdg_paths
from ovid2.dataspace.path import OdsPathError
from ovid2.dataspace.resolver import OdsResolutionError
from ovid2.web.models import (
    OdsColumnModel,
    OdsTableCount,
    OdsTableDetail,
    OdsTableSample,
)
from ovid2.web.services._broker import BrokerError, run_tool_json

logger = logging.getLogger(__name__)

__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_TTL_SECONDS",
    "ExportFile",
    "TableServiceError",
    "count_rows",
    "describe_table",
    "export_dir",
    "export_limits",
    "export_table",
    "prune_exports",
    "sample_table",
]

#: Formats the explorer offers (mirror of :data:`ovid2.exports.EXPORT_FORMATS`
#: — duplicated deliberately so the web tier never imports the writers).
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "csv": ("csv", "text/csv; charset=utf-8"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pdf": ("pdf", "application/pdf"),
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}

#: Seconds an export file stays downloadable.
EXPORT_TTL_SECONDS = 3600

#: Detail-cache lifetime (seconds) — the page re-requests on tab switches.
_CACHE_TTL_SECONDS = 60.0

_CACHE: dict[str, tuple[float, OdsTableDetail]] = {}
_CACHE_LOCK = threading.Lock()


class TableServiceError(ValueError):
    """User-facing error (bad path, unknown account, failed operation)."""


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def export_limits() -> dict[str, int]:
    """Return the per-format row cap of a synchronous (download-now) export.

    Overridable with ``$OVID2_EXPORT_MAX_ROWS`` (csv / xlsx) and
    ``$OVID2_EXPORT_DOC_MAX_ROWS`` (pdf / docx).
    """
    tabular = _env_int("OVID2_EXPORT_MAX_ROWS", 100_000)
    document = _env_int("OVID2_EXPORT_DOC_MAX_ROWS", 5_000)
    return {"csv": tabular, "xlsx": min(tabular, 1_048_575), "pdf": document, "docx": document}


def _deadline(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _resolve_table(path: str) -> tuple[str, str, str]:
    """Return ``(connection, type, location)`` for a Data Space table path.

    Raises:
        TableServiceError: Malformed path, unknown account, or the account root.
    """
    try:
        target = dataspace.resolve(path)
    except (OdsPathError, OdsResolutionError) as exc:
        raise TableServiceError(str(exc)) from exc
    if target.connection is None or not target.location:
        raise TableServiceError("A table path is /<account>/<folder…>/<table>.")
    return target.connection, target.type, target.location


def _run(tool_args: list[str], timeout: float) -> dict[str, Any]:
    try:
        return run_tool_json(tool_args, timeout=timeout)
    except BrokerError as exc:
        logger.warning("Data Space table op %s failed: %s", tool_args[:1], exc)
        raise TableServiceError(str(exc)) from exc


def _check(result: dict[str, Any]) -> None:
    if not result.get("ok"):
        raise TableServiceError(str(result.get("message") or "Operation failed."))


# ---------------------------------------------------------------------------
# Describe / sample / count
# ---------------------------------------------------------------------------


def describe_table(path: str, *, refresh: bool = False) -> OdsTableDetail:
    """Return the table detail (stats, fields, DDL) for a Data Space path."""
    connection, type_name, location = _resolve_table(path)
    key = f"{connection}\x00{location}".lower()
    now = time.monotonic()
    if not refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    result = _run(
        ["describe-table", connection, "--path", location],
        _deadline("OVID2_BROWSE_DEADLINE_SECONDS", 300.0),
    )
    if result.get("ok") and not result.get("supported", True):
        detail = OdsTableDetail(
            path=path,
            connection=connection,
            type=type_name,
            location=location,
            name=location.rsplit("/", 1)[-1],
            supported=False,
            previewable=False,
            message=result.get("message"),
            export_limits=export_limits(),
        )
        return detail
    _check(result)
    table = dict(result.get("table") or {})
    detail = OdsTableDetail(
        path=path,
        connection=connection,
        type=type_name or str(result.get("type") or ""),
        location=location,
        name=str(table.get("name") or location.rsplit("/", 1)[-1]),
        kind=str(table.get("kind") or "table"),
        format=str(table.get("format") or "unknown"),
        columns=[OdsColumnModel(**c) for c in table.get("columns") or []],
        row_count=table.get("row_count"),
        row_count_exact=bool(table.get("row_count_exact", True)),
        size_bytes=table.get("size_bytes"),
        modified_at=table.get("modified_at"),
        created_at=table.get("created_at"),
        ddl=table.get("ddl"),
        delimiter=table.get("delimiter"),
        parts=table.get("parts"),
        previewable=bool(table.get("previewable", True)),
        message=table.get("message"),
        export_limits=export_limits(),
    )
    with _CACHE_LOCK:
        _CACHE[key] = (now, detail)
    return detail


def sample_table(path: str, limit: int = 100) -> OdsTableSample:
    """Return the first ``limit`` (≤1000) rows of the table at a Data Space path."""
    connection, _type, location = _resolve_table(path)
    limit = max(1, min(int(limit), 1000))
    result = _run(
        ["sample-table", connection, "--path", location, "--limit", str(limit)],
        _deadline("OVID2_BROWSE_DEADLINE_SECONDS", 300.0),
    )
    if result.get("ok") and not result.get("supported", True):
        return OdsTableSample(path=path, supported=False, message=result.get("message"))
    _check(result)
    sample = dict(result.get("sample") or {})
    return OdsTableSample(
        path=path,
        columns=[str(c) for c in sample.get("columns") or []],
        rows=list(sample.get("rows") or []),
        truncated=bool(sample.get("truncated")),
        limit=limit,
    )


def count_rows(path: str) -> OdsTableCount:
    """Return the exact row count (may scan the data — operator-triggered)."""
    connection, _type, location = _resolve_table(path)
    result = _run(
        ["count-rows", connection, "--path", location],
        _deadline("OVID2_COUNT_DEADLINE_SECONDS", 900.0),
    )
    if result.get("ok") and not result.get("supported", True):
        return OdsTableCount(path=path, supported=False, message=result.get("message"))
    _check(result)
    count = int(result.get("row_count") or 0)
    with _CACHE_LOCK:
        hit = _CACHE.get(f"{connection}\x00{location}".lower())
        if hit:
            _CACHE[f"{connection}\x00{location}".lower()] = (
                hit[0],
                hit[1].model_copy(update={"row_count": count, "row_count_exact": True}),
            )
    return OdsTableCount(path=path, row_count=count, exact=True)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_dir() -> Path:
    """Return the directory holding downloadable exports (mode 0700)."""
    override = os.environ.get("OVID2_WEB_EXPORT_DIR")
    base = Path(override) if override else xdg_paths.state_dir() / "web_exports"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def prune_exports(max_age: float = EXPORT_TTL_SECONDS) -> int:
    """Delete export folders older than ``max_age`` seconds; return the count."""
    removed = 0
    cutoff = time.time() - max_age
    try:
        entries = list(export_dir().iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class ExportFile:
    """A finished export ready to stream to the browser."""

    def __init__(
        self, path: Path, filename: str, media_type: str, rows: int, truncated: bool
    ) -> None:
        self.path = path
        self.filename = filename
        self.media_type = media_type
        self.rows = rows
        self.truncated = truncated


_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


def _download_name(location: str, fmt: str) -> str:
    """Return a safe download filename (``<table>.<ext>``)."""
    leaf = location.rstrip("/").rsplit("/", 1)[-1]
    from ovid2.drivers._table_listing import strip_table_extension

    stem = _SAFE_STEM.sub("_", strip_table_extension(leaf)).strip("._") or "export"
    return f"{stem}.{EXPORT_FORMATS[fmt][0]}"


def _normalise_delimiter(delimiter: str) -> str:
    raw = delimiter or ","
    if raw.lower() in ("tab", "\\t", "\t"):
        return "tab"
    if len(raw) != 1 or raw in ('"', "\r", "\n"):
        raise TableServiceError("The CSV delimiter must be a single character (or 'tab').")
    return raw


def export_table(
    path: str,
    *,
    fmt: str = "csv",
    delimiter: str = ",",
    header_case: str = "asis",
    limit: int | None = None,
    actor: str = "web",
) -> ExportFile:
    """Export the table at a Data Space path and return the finished file.

    The row count is capped at :func:`export_limits` for ``fmt`` (a
    larger request is clamped); full-table exports go through the
    background ``export`` job.

    Raises:
        TableServiceError: Bad options, a failed export, or no file produced.
    """
    if fmt not in EXPORT_FORMATS:
        raise TableServiceError(f"Unknown export format {fmt!r}; choose csv / xlsx / pdf / docx.")
    if header_case not in ("asis", "upper", "lower"):
        raise TableServiceError("header_case must be asis / upper / lower.")
    connection, _type, location = _resolve_table(path)
    cap = export_limits()[fmt]
    rows = cap if limit is None else max(1, min(int(limit), cap))
    delim = _normalise_delimiter(delimiter) if fmt == "csv" else ","
    prune_exports()
    folder = export_dir() / secrets.token_hex(12)
    folder.mkdir(mode=0o700)
    filename = _download_name(location, fmt)
    out = folder / filename
    result = _run(
        [
            "export-table",
            connection,
            "--path",
            location,
            "--format",
            fmt,
            "--delimiter",
            delim,
            "--header-case",
            header_case,
            "--limit",
            str(rows),
            "--out",
            str(out),
        ],
        _deadline("OVID2_EXPORT_DEADLINE_SECONDS", 900.0),
    )
    if result.get("ok") and not result.get("supported", True):
        shutil.rmtree(folder, ignore_errors=True)
        raise TableServiceError(str(result.get("message") or "Export is not available here."))
    if not result.get("ok") or not out.is_file():
        shutil.rmtree(folder, ignore_errors=True)
        raise TableServiceError(str(result.get("message") or "Export failed."))
    info = dict(result.get("export") or {})
    from ovid2.web import db as web_db

    web_db.audit(
        actor=actor,
        action="dataspace.export",
        target=path,
        detail_json=json.dumps(
            {"format": fmt, "rows": int(info.get("rows") or 0), "header_case": header_case}
        ),
    )
    return ExportFile(
        path=out,
        filename=filename,
        media_type=EXPORT_FORMATS[fmt][1],
        rows=int(info.get("rows") or 0),
        truncated=bool(info.get("truncated")),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the ovid2 web routes; not a script.")
