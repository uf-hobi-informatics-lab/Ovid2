"""Address-is-exact-location helpers shared by every driver and tier.

Ovid2 is a processor: a command plus an *input address* and an *output
address*. Every address — disc, object store, or warehouse — is the
**exact** location the data is read from or written to:

- folder stores (disc / s3 / azure_blob / gcs): the connection root
  (``host_path`` / bucket / container) plus ``location.path`` is the
  directory that directly holds the tables;
- warehouses (snowflake / databricks / postgres / mssql): the
  ``database`` (or ``catalog``) plus ``schema`` named by the location.

Neither ``pipeline`` nor ``batch_id`` is ever appended to a storage
path. The ``batch_id`` *is* the complete Data Space path of the input
location — the storage account (connection) followed by the location
path (``inbox/landing/2026_03``, ``sf_prod/CHAR_DB/SITE_X``); it names
the run, it is never appended to an address. See
the adr-address-is-exact-location design note.

Import-safe: stdlib only, so the web tier, the MCP server, the CLI and
every driver module can use it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "WAREHOUSE_TYPES",
    "BatchPathError",
    "dataspace_path",
    "location_path",
    "location_with_path",
    "normalize_batch_path",
    "split_dataspace_path",
    "sql_ident",
    "warehouse_db_schema",
]

#: Connection types whose location is ``DATABASE[/SCHEMA]`` rather than a
#: directory path. ``databricks`` uses ``catalog`` in place of ``database``.
WAREHOUSE_TYPES: frozenset[str] = frozenset({"snowflake", "databricks", "postgres", "mssql"})

#: An unquoted SQL identifier (Snowflake / ANSI rules).
_UNQUOTED_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class BatchPathError(ValueError):
    """Raised when a batch path / location path is malformed."""


def normalize_batch_path(raw: str | None, *, warehouse: bool = False) -> str:
    """Return a canonical relative location path.

    Leading / trailing / duplicate slashes are collapsed. Multi-segment
    paths are allowed for folder stores. Warehouse paths must be
    ``DATABASE`` or ``DATABASE/SCHEMA``.

    Args:
        raw: The operator-supplied path (``/landing//2026_03/``).
        warehouse: ``True`` for warehouse connection types.

    Returns:
        The normalised path (``landing/2026_03``); ``""`` for empty input.

    Raises:
        BatchPathError: On ``..`` / ``.`` segments, backslashes, NUL
            bytes, or a warehouse path deeper than two segments.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "\\" in text or "\x00" in text:
        raise BatchPathError(f"batch path {raw!r} may not contain backslashes or NUL bytes.")
    parts = [p.strip() for p in text.split("/") if p.strip()]
    for part in parts:
        if part in (".", ".."):
            raise BatchPathError(f"batch path {raw!r} may not contain '.' or '..' segments.")
    if warehouse and len(parts) > 2:
        raise BatchPathError(
            f"warehouse batch path {raw!r} must be DATABASE or DATABASE/SCHEMA "
            f"(got {len(parts)} segments)."
        )
    return "/".join(parts)


def warehouse_db_schema(location: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return ``(database, schema)`` for a warehouse location.

    Explicit ``database`` (or ``catalog``) / ``schema`` keys win over a
    ``path`` of the form ``DATABASE[/SCHEMA]``. Either element is ``""``
    when absent — there is no implicit ``PUBLIC`` default here.
    """
    loc = dict(location or {})
    parts = [p.strip() for p in str(loc.get("path") or "").split("/") if p.strip()]
    database = str(loc.get("database") or loc.get("catalog") or (parts[0] if parts else ""))
    schema = str(loc.get("schema") or (parts[1] if len(parts) > 1 else ""))
    return database.strip(), schema.strip()


def location_path(location: Mapping[str, Any] | None, *, warehouse: bool = False) -> str:
    """Return the canonical path a location points at (its *batch path*).

    Folder stores: ``location.path``. Warehouses: ``DATABASE/SCHEMA``
    assembled from explicit keys or ``path``.
    """
    if warehouse:
        database, schema = warehouse_db_schema(location)
        return "/".join(p for p in (database, schema) if p)
    return normalize_batch_path(str((location or {}).get("path") or ""))


def location_with_path(
    location: Mapping[str, Any] | None,
    path: str,
    *,
    warehouse: bool = False,
) -> dict[str, Any]:
    """Return a copy of ``location`` re-pointed at ``path``.

    Non-path knobs (``format``, ``single_file``, ``cdm`` …) are kept.
    For warehouses the explicit ``database`` / ``catalog`` / ``schema``
    keys are dropped so ``path`` is the single source of truth.
    """
    out = dict(location or {})
    for key in ("path", "database", "catalog", "schema") if warehouse else ("path",):
        out.pop(key, None)
    norm = normalize_batch_path(path, warehouse=warehouse)
    if norm:
        out["path"] = norm
    return out


def dataspace_path(connection: str, path: str = "") -> str:
    """Return the Data Space path ``<connection>/<path>`` of a location.

    The storage account (connection) is just the top folder of the Data
    Space, so the *complete* path of any folder / schema is the
    connection name followed by its location path. This is the canonical
    ``batch_id`` of a run reading from that location.
    """
    conn = str(connection or "").strip().strip("/")
    if not conn:
        raise BatchPathError("a Data Space path needs a storage account (connection) name.")
    rest = normalize_batch_path(path)
    return f"{conn}/{rest}" if rest else conn


def split_dataspace_path(full: str) -> tuple[str, str]:
    """Split ``<connection>/<path>`` into ``(connection, path)``.

    Raises:
        BatchPathError: On an empty path or ``..`` / ``.`` segments.
    """
    norm = normalize_batch_path(full)
    if not norm:
        raise BatchPathError("empty Data Space path.")
    head, _, rest = norm.partition("/")
    return head, rest


def sql_ident(name: str) -> str:
    """Return ``name`` as a SQL identifier, double-quoting when required.

    Plain identifiers (``SITE_X``) pass through unquoted (Snowflake
    upper-cases them). Anything else (``20260101``, ``my-schema``) is
    wrapped in double quotes with embedded quotes doubled, so a
    date-shaped batch schema is a valid target.

    Raises:
        BatchPathError: When ``name`` is empty.
    """
    text = str(name or "").strip()
    if not text:
        raise BatchPathError("empty SQL identifier")
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text
    if _UNQUOTED_IDENT_RE.match(text):
        return text
    return '"' + text.replace('"', '""') + '"'


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
