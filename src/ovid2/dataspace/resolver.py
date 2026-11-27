"""ODS resolver — the Data Space as plain directories.

Every data space is the same: a tree of directories rooted at a storage
account (connection). ``/<connection>/<path…>`` names a folder (disc /
s3 / azure_blob / gcs) or a ``DATABASE/SCHEMA[/TABLE]`` (warehouses).

Public API:

- :func:`resolve` — map a path to the storage account + exact location
  it names, and to the saved address (if any) pointing exactly there.
- :func:`list_children` — list one level under a path. Callers supply a
  ``lister(connection, prefix)`` so the web tier can broker the
  credential-bearing ``list_dir`` to an executor subprocess while the
  CLI calls the driver directly (:func:`driver_lister`).
- :func:`list_storage_accounts` — the root listing.

The resolver performs no data I/O and imports no pyspark.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ovid2.config import addresses as _address_config
from ovid2.config import connections as _connection_config
from ovid2.dataspace.path import OdsPath

logger = logging.getLogger(__name__)

__all__ = [
    "AccountSuggestion",
    "OdsEntry",
    "OdsResolutionError",
    "ResolvedTarget",
    "driver_lister",
    "list_children",
    "list_storage_accounts",
    "resolve",
    "suggest_accounts",
]

#: Connection types whose location is ``DATABASE/SCHEMA`` (not a folder).
_WAREHOUSES: frozenset[str] = frozenset({"snowflake", "databricks", "postgres", "mssql"})


class OdsResolutionError(ValueError):
    """Raised when a path cannot be mapped to a storage account."""


@dataclass(frozen=True)
class OdsEntry:
    """One child in a Data Space listing.

    Attributes:
        name: The component name.
        kind: ``account`` | ``dir`` | ``file`` | ``table``.
        path: The child's full Data Space path (``/<conn>/<…>``).
        type: The storage type (``disc`` / ``snowflake`` / …).
        size_bytes: Size when the store reports it.
        modified_at: ISO-8601 UTC last-modified time when reported.
        row_count: Row count when the store reports it for free.
    """

    name: str
    kind: str
    path: str
    type: str = ""
    size_bytes: int | None = None
    modified_at: str | None = None
    row_count: int | None = None


@dataclass(frozen=True)
class ResolvedTarget:
    """The outcome of :func:`resolve`.

    Attributes:
        path: The normalised path.
        connection: The storage account (``None`` for the root).
        type: The storage type.
        location: The exact location under the account (``''`` = top).
        address_name: A saved address pointing *exactly* at this
            location, when one exists.
        batch_id: The batch id a run reading this location gets
            (``<connection>/<location>``).
        candidates: Every saved address on this storage account.
    """

    path: OdsPath
    connection: str | None
    type: str
    location: str
    address_name: str | None = None
    batch_id: str = ""
    candidates: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connection_type(name: str) -> str | None:
    """Return the storage type of a connection config, or ``None``."""
    try:
        raw = _connection_config.load_connection_config(name)
    except (FileNotFoundError, ValueError):
        return None
    return str(raw.get("type") or "") or None


def list_storage_accounts() -> list[OdsEntry]:
    """Return every storage account (connection) — the Data Space root."""
    entries: list[OdsEntry] = []
    for name in sorted(_connection_config.list_available_connections()):
        entries.append(
            OdsEntry(name=name, kind="account", path=f"/{name}", type=_connection_type(name) or "")
        )
    return entries


def _address_location_path(config: dict[str, Any], warehouse: bool) -> str:
    """Return the canonical location path stored on a connection-shape address."""
    from ovid2.drivers._layout import location_path

    return location_path(dict(config.get("location") or {}), warehouse=warehouse)


def _addresses_on(connection: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(name, config)`` for every connection-shape address on ``connection``."""
    out: list[tuple[str, dict[str, Any]]] = []
    for name in _address_config.list_available_addresses():
        try:
            cfg = _address_config.load_address_config(name)
        except (FileNotFoundError, ValueError):
            continue
        if cfg.get("_shape") == "connection" and str(cfg.get("connection")) == connection:
            out.append((name, cfg))
    return out


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _norm(name: str) -> str:
    """Lower-case ``name`` and drop separators, for fuzzy comparison."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass(frozen=True)
class AccountSuggestion:
    """A storage account that *could* serve a path naming an unknown one.

    Attributes:
        name: Proposed connection name.
        type: Storage type (always ``disc`` for derived proposals).
        host_path: Folder the new account would point at.
        sibling_of: The existing account the proposal was derived from.
    """

    name: str
    type: str
    host_path: str
    sibling_of: str


def suggest_accounts(
    path: str | OdsPath, limit: int = 3
) -> tuple[list[str], list[AccountSuggestion]]:
    """Suggest storage accounts for a path whose account does not exist.

    Reads connection configs only (no secrets, no storage listing).

    Returns:
        ``(close, proposals)``: existing account names close to the unknown
        one (prefix / containment / fuzzy), and new ``disc`` accounts derived
        from an existing disc account whose ``host_path`` has the unknown
        name as a parent folder — e.g. ``/inboxarchive/NU`` with an account
        on ``/data/inboxarchive/ACME`` proposes ``inboxarchive_nu`` on
        ``/data/inboxarchive/NU``.
    """
    import difflib
    import posixpath

    ods_path = path if isinstance(path, OdsPath) else OdsPath.parse(path)
    wanted = ods_path.connection or ""
    key = _norm(wanted)
    names = _connection_config.list_available_connections()
    close: list[str] = []
    for cand in names:
        ck = _norm(cand)
        if key and (ck.startswith(key) or key.startswith(ck) or (len(key) > 3 and key in ck)):
            close.append(cand)
    by_norm = {_norm(n): n for n in names}
    for hit in difflib.get_close_matches(key, list(by_norm), n=limit, cutoff=0.6):
        if by_norm[hit] not in close:
            close.append(by_norm[hit])
    proposals: list[AccountSuggestion] = []
    seen: set[str] = set()
    first = ods_path.parts[0] if ods_path.parts else ""
    for cand in names:
        try:
            raw = _connection_config.load_connection_config(cand)
        except (FileNotFoundError, ValueError):
            continue
        if raw.get("type") != "disc":
            continue
        host = str((raw.get("args") or {}).get("host_path") or "").rstrip("/")
        if not host:
            continue
        segs = host.split("/")
        for idx, seg in enumerate(segs):
            if not seg or seg.lower() != wanted.lower():
                continue
            base = "/".join(segs[: idx + 1])
            target = posixpath.join(base, first) if first else base
            suffix = f"_{_norm(first)}" if first else ""
            name = f"{seg}{suffix}".lower()
            if target in seen or name in names:
                continue
            seen.add(target)
            proposals.append(
                AccountSuggestion(name=name, type="disc", host_path=target, sibling_of=cand)
            )
    return close[:limit], proposals[:limit]


def _unknown_account_message(ods_path: OdsPath) -> str:
    """Build the error text for a path whose storage account is unknown."""
    msg = f"Storage account '{ods_path.connection}' does not exist."
    close, proposals = suggest_accounts(ods_path)
    if close:
        msg += " Close existing accounts: " + ", ".join(close) + "."
    for p in proposals:
        msg += (
            f" Suggested new {p.type} account '{p.name}' with host_path '{p.host_path}'"
            f" (next to '{p.sibling_of}')."
        )
    return msg


def resolve(path: str | OdsPath) -> ResolvedTarget:
    """Map a Data Space path to its storage account + exact location.

    Args:
        path: ``/<connection>/<location…>`` (an :class:`OdsPath` or string).

    Raises:
        OdsPathError: When ``path`` fails the grammar.
        OdsResolutionError: When the storage account does not exist.
    """
    ods_path = path if isinstance(path, OdsPath) else OdsPath.parse(path)
    if ods_path.connection is None:
        return ResolvedTarget(path=ods_path, connection=None, type="", location="")
    conn = ods_path.connection
    conn_type = _connection_type(conn)
    if conn_type is None:
        # Case-insensitive fallback: the path equality is case-insensitive.
        for candidate in _connection_config.list_available_connections():
            if candidate.lower() == conn.lower():
                conn, conn_type = candidate, _connection_type(candidate)
                break
    if conn_type is None:
        raise OdsResolutionError(_unknown_account_message(ods_path))
    warehouse = conn_type in _WAREHOUSES
    location = ods_path.location
    match: str | None = None
    names: list[str] = []
    for name, cfg in _addresses_on(conn):
        names.append(name)
        if match is None and _address_location_path(cfg, warehouse).lower() == location.lower():
            match = name
    return ResolvedTarget(
        path=OdsPath(connection=conn, parts=ods_path.parts),
        connection=conn,
        type=conn_type,
        location=location,
        address_name=match,
        batch_id=OdsPath(connection=conn, parts=ods_path.parts).batch_id(),
        candidates=tuple(sorted(names)),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

#: ``lister(connection, prefix) -> (supported, entries, message)``. Each
#: entry is a mapping with ``name`` / ``kind`` / ``path`` (path relative to
#: the storage account) and optional ``size_bytes``.
Lister = Callable[[str, str | None], tuple[bool, Sequence[dict[str, Any]], str | None]]


def driver_lister(
    connection: str, prefix: str | None
) -> tuple[bool, list[dict[str, Any]], str | None]:
    """Default :data:`Lister` — call the storage driver's ``list_dir`` directly.

    Executor-tier only (resolves the connection's secrets). The web tier
    passes a broker-backed lister instead.
    """
    from ovid2.drivers.registry import DRIVERS, instantiate

    raw = _connection_config.load_connection_config(connection)
    type_name = str(raw.get("type") or "")
    if type_name not in DRIVERS:
        return False, [], f"storage type '{type_name}' is not registered"
    driver = instantiate(
        type_name,
        args=dict(raw.get("args") or {}),
        secrets=dict(_connection_config.resolve_secrets(raw)),
    )
    try:
        entries = driver.list_dir(prefix)
    except NotImplementedError as exc:
        return False, [], str(exc)
    return True, [e.to_dict() for e in entries or []], None


def list_children(
    path: str | OdsPath,
    *,
    lister: Lister,
) -> tuple[bool, list[OdsEntry], str | None]:
    """List one level under a Data Space path.

    ``/`` lists storage accounts; anything deeper lists the folder /
    schema through the storage account's ``list_dir``.

    ``lister`` is **required** (no default): the executor passes
    :func:`driver_lister` (resolves secrets in-process); the web tier
    passes its broker-backed lister. A default would let control-plane
    code dereference credentials in-process by omission.

    Returns:
        ``(supported, entries, message)``.

    Raises:
        OdsPathError / OdsResolutionError: On a bad path / unknown account.
    """
    target = resolve(path)
    if target.connection is None:
        return True, list_storage_accounts(), None
    supported, raw_entries, message = lister(target.connection, target.location or None)
    entries: list[OdsEntry] = []
    for raw in raw_entries:
        rel = str(raw.get("path") or raw.get("name") or "").strip("/")
        size = raw.get("size_bytes")
        rows = raw.get("row_count")
        modified = raw.get("modified_at")
        entries.append(
            OdsEntry(
                name=str(raw.get("name") or rel.rsplit("/", 1)[-1]),
                kind=str(raw.get("kind") or "dir"),
                path=f"/{target.connection}/{rel}" if rel else f"/{target.connection}",
                type=target.type,
                size_bytes=int(size) if isinstance(size, int) else None,
                modified_at=str(modified) if modified else None,
                row_count=int(rows) if isinstance(rows, int) else None,
            )
        )
    return supported, entries, message


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
