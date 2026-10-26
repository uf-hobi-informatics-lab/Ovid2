"""Resolve the concrete table roster of a copy/move *source*.

``copy`` / ``move`` jobs read every named table out of a self-describing
container (today: a Microsoft Access ``.accdb`` / ``.mdb`` file) and write
them verbatim to the output. Unlike an ETL run — whose table roster comes
from a CDM handle — a copy source's tables live *inside the file*, so the
submit-page picker and the run-page pre-seed must enumerate the file
itself.

This module answers "what tables does this source contain?" without ever
resolving a plaintext secret in the web process: enumeration is delegated
to the same short-lived executor subprocess the storage browser uses
(``ovid2 tools browse-storage <connection> --prefix <file>`` via
:func:`ovid2.web.services.storage_browse_svc.browse_storage`). Only the
*static* address→(connection, location) resolution happens here, and that
reads config JSON only (trust-model requirement:
``tests/contract/test_web_no_plaintext_secret_reads.py``).

Two entry points, mirroring the two ways the operator picks a source:

- :func:`tables_for_address` — a *saved address* name (the submit-page
  "Saved address" mode and the run-page, which stores the resolved
  address on the job).
- :func:`tables_for_connection` — an inline *storage account* +
  browsed *location* (the submit-page "Browse storage" mode).

Both return ``([table names], supported)``. ``supported`` is ``False``
when the source's driver type is not a self-describing table container
(e.g. a plain disc folder or a Snowflake schema); callers then fall back
to their existing behaviour (an explicit typed list / the CDM plan).
"""

from __future__ import annotations

import json
import logging

from ovid2.config import addresses as _addresses

logger = logging.getLogger(__name__)

__all__ = [
    "tables_for_address",
    "tables_for_connection",
]

#: Driver types whose sources are self-describing table containers whose
#: tables can be enumerated by browsing *into* the source. Kept as a
#: small allow-list (rather than a driver-flag probe) so the web tier
#: stays import-light; extend it when a new self-describing source lands.
_ENUMERABLE_SOURCE_TYPES: frozenset[str] = frozenset({"access"})


def _connection_and_prefix_for_address(
    address_name: str,
) -> tuple[str, str] | None:
    """Return ``(connection_name, browse_prefix)`` for a saved address.

    Reads only the address JSON (config-only, no secret). Returns
    ``None`` when the address is legacy-shaped (no connection), missing,
    or its connection type is not an enumerable source.

    The browse prefix is the address's ``location.path`` — the sub-path
    of the ``.accdb`` / ``.mdb`` file relative to the connection's
    browse root, i.e. exactly what
    :meth:`ovid2.drivers.access.driver.AccessDriver.list_dir` descends
    into to list the file's tables.
    """
    name = address_name.strip()
    if not name:
        return None
    try:
        config = _addresses.load_address_config(name)
    except (FileNotFoundError, ValueError):
        return None
    # Legacy-shape addresses carry no connection; they are not browseable
    # storage accounts, so we cannot enumerate them this way.
    if config.get("_shape") == "legacy":
        return None
    connection_name = config.get("connection")
    if not connection_name:
        return None
    conn_type = _connection_type(str(connection_name))
    if conn_type not in _ENUMERABLE_SOURCE_TYPES:
        return None
    location = config.get("location") or {}
    prefix = ""
    if isinstance(location, dict):
        prefix = str(location.get("path") or "")
    return str(connection_name), prefix


def _connection_type(connection_name: str) -> str | None:
    """Return the driver ``type`` of a connection (config-only)."""
    try:
        from ovid2.config import connections as _connections

        path_conn = _connections.locate_connection(connection_name)
        if path_conn is None:
            return None
        with path_conn.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return str(payload.get("type", "")) or None


def _enumerate(connection_name: str, prefix: str) -> list[str]:
    """Browse ``connection_name`` at ``prefix`` and return table names.

    Delegates to :func:`storage_browse_svc.browse_storage` (the
    trust-safe executor hand-off). Only ``kind == 'table'`` entries are
    returned; a failed / unsupported browse yields an empty list.
    """
    from ovid2.web.services import storage_browse_svc

    try:
        result = storage_browse_svc.browse_storage(connection_name, prefix or None)
    except storage_browse_svc.StorageBrowseError as exc:
        logger.warning(
            "source-tables browse of '%s' (%s) failed: %s",
            connection_name,
            prefix,
            exc,
        )
        return []
    if not getattr(result, "supported", False):
        return []
    names = [e.name for e in (result.entries or []) if getattr(e, "kind", "") == "table" and e.name]
    return names


def tables_for_address(address_name: str) -> tuple[list[str], bool]:
    """Return ``(tables, supported)`` for a saved copy/move source address.

    ``supported`` is ``True`` only when the address resolves to an
    enumerable self-describing source (e.g. Access). When ``True`` but
    the returned list is empty, enumeration was attempted but produced
    nothing (unreadable file / jars unavailable in the web tier); the
    caller should keep its existing fallback rather than treating "no
    tables" as authoritative.
    """
    resolved = _connection_and_prefix_for_address(address_name)
    if resolved is None:
        return [], False
    connection_name, prefix = resolved
    return _enumerate(connection_name, prefix), True


def tables_for_connection(connection_name: str, location: str) -> tuple[list[str], bool]:
    """Return ``(tables, supported)`` for an inline storage account + file.

    ``location`` is the browsed path of the ``.accdb`` / ``.mdb`` file
    relative to the connection root (the submit-page "Browse storage"
    hidden ``*_location`` value).
    """
    if not connection_name.strip():
        return [], False
    conn_type = _connection_type(connection_name.strip())
    if conn_type not in _ENUMERABLE_SOURCE_TYPES:
        return [], False
    return _enumerate(connection_name.strip(), location.strip()), True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
