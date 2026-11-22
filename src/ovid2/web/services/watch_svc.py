"""Watch-trigger scan service (pipeline-not-pipeline Phase B).

Event-driven new-batch detection for ``watch``-trigger pipelines. The
web-worker calls :func:`scan_for_new_batches` on each tick for every
enabled ``watch`` pipeline; each returned batch id is submitted as a job
and then :func:`record_seen` advances the pipeline's cursor so the same
folder is never re-submitted.

Cursors live in the additive ``web_watch_cursors`` table (Phase A),
keyed by ``(pipeline, input_address)``. Every address is the exact
location of its data, so a watch pipeline's input address names the
*parent* folder (or warehouse database) being watched; each child folder
(or schema) is one batch. "New" is defined by lexical ordering of those
child names: any directory entry whose name sorts strictly after the
stored ``last_seen_batch`` is unseen. Each new batch runs against a
child address pointing exactly at that folder / schema
(:func:`batch_input_address`). Batch folders in Ovid2 are
date-shaped (``2024_01`` / ``20240115``) and therefore lexically
monotonic, so this is a total order without parsing.

Trust / isolation: this module is web-tier and MUST NOT import
``ovid2.core`` or pyspark (enforced by ``test_web_no_pipeline_imports``).
Folder listing is delegated to :mod:`storage_browse_svc`, which brokers
the actual credential-bearing ``list_dir`` to a short-lived executor
subprocess.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ovid2.web import db as web_db
from ovid2.web.models import PipelineConfigDetail
from ovid2.web.services import addresses_svc, storage_browse_svc

logger = logging.getLogger(__name__)

__all__ = [
    "batch_input_address",
    "child_address",
    "last_checked_at",
    "last_seen",
    "record_seen",
    "scan_for_new_batches",
    "scan_location",
]


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def last_checked_at(pipeline: str, input_address: str) -> str | None:
    """Return the stored ``last_checked_at`` ISO timestamp, or ``None``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT last_checked_at FROM web_watch_cursors "
            "WHERE pipeline = ? AND input_address = ?",
            (pipeline, input_address),
        ).fetchone()
    if row is None:
        return None
    value = row["last_checked_at"]
    return str(value) if value is not None else None


def last_seen(pipeline: str, input_address: str) -> str | None:
    """Return the stored ``last_seen_batch`` for a pipeline input, or ``None``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT last_seen_batch FROM web_watch_cursors "
            "WHERE pipeline = ? AND input_address = ?",
            (pipeline, input_address),
        ).fetchone()
    if row is None:
        return None
    value = row["last_seen_batch"]
    return str(value) if value is not None else None


def record_seen(pipeline: str, input_address: str, batch_id: str) -> None:
    """Advance the cursor so ``batch_id`` (and anything before it) is seen.

    The cursor only moves forward: a lexically-smaller ``batch_id`` than
    the stored value leaves the cursor unchanged (defensive against
    out-of-order dispatch). ``last_checked_at`` is always refreshed.
    """
    web_db.ensure_schema()
    now = _now_iso()
    current = last_seen(pipeline, input_address)
    new_value = batch_id if current is None else max(current, batch_id)
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_watch_cursors "
            "(pipeline, input_address, last_seen_batch, last_checked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pipeline, input_address) DO UPDATE SET "
            "last_seen_batch = excluded.last_seen_batch, "
            "last_checked_at = excluded.last_checked_at",
            (pipeline, input_address, new_value, now),
        )


def _touch_checked(pipeline: str, input_address: str) -> None:
    """Record that a scan ran, without moving the cursor."""
    web_db.ensure_schema()
    now = _now_iso()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_watch_cursors "
            "(pipeline, input_address, last_seen_batch, last_checked_at) "
            "VALUES (?, ?, NULL, ?) "
            "ON CONFLICT(pipeline, input_address) DO UPDATE SET "
            "last_checked_at = excluded.last_checked_at",
            (pipeline, input_address, now),
        )


def _watched_prefix(address: object) -> str:
    """Return the location path a watch input address points at."""
    from ovid2.drivers._layout import location_path

    connection = str(getattr(address, "connection", "") or "")
    warehouse = addresses_svc.is_warehouse_connection(connection)
    return location_path(getattr(address, "location", None) or {}, warehouse=warehouse)


def child_address(input_address: str, folder: str, *, actor: str) -> str:
    """Return (resolving or creating) the address of ``<input>/<folder>``.

    The watched input address names the parent location; the batch's
    address points exactly at ``<parent>/<folder>`` on the same storage
    account.

    Raises:
        addresses_svc.AddressError: When the parent address is not a
            connection-shape address or the child cannot be created.
    """
    address = addresses_svc.get_address(input_address)
    if address is None or not address.connection:
        raise addresses_svc.AddressError(
            f"watch input '{input_address}' is not a connection-shape address."
        )
    parent = _watched_prefix(address)
    child_path = "/".join(p for p in (parent, folder.strip("/")) if p)
    location = {
        k: v
        for k, v in (address.location or {}).items()
        if k not in ("path", "database", "catalog", "schema")
    }
    location["path"] = child_path
    return addresses_svc.resolve_or_create_address(address.connection, location, actor=actor)


def batch_input_address(detail: PipelineConfigDetail, folder: str, *, actor: str) -> str:
    """Return the address for one watched batch of a pipeline (see :func:`child_address`)."""
    return child_address(detail.input_address, folder, actor=actor)


def scan_location(cursor_key: str, input_address: str) -> list[str]:
    """Return the unseen batch folders directly under ``input_address``.

    ``cursor_key`` names the cursor row (a pipeline name, or
    ``workflow:<name>`` for a watch-triggered workflow). Lists one level
    of the input *location* and returns every ``dir`` entry whose name
    sorts strictly after the stored ``last_seen_batch``, ascending. With
    no cursor yet every present folder is unseen.

    Returns an empty list when the input address is unresolvable, is a
    legacy (connectionless) address, the driver cannot list directories,
    or the listing fails — never raises, so one misbehaving watcher
    cannot break the worker tick.
    """
    address = addresses_svc.get_address(input_address)
    if address is None or not address.connection:
        logger.warning(
            "watch %s: input_address %r is not a connection-shape address; skipping scan.",
            cursor_key,
            input_address,
        )
        return []
    try:
        response = storage_browse_svc.browse_storage(
            address.connection, _watched_prefix(address) or None
        )
    except storage_browse_svc.StorageBrowseError as exc:
        logger.warning("watch %s: browse failed: %s", cursor_key, exc)
        return []
    if not response.supported:
        logger.warning(
            "watch %s: storage account %r cannot list directories.",
            cursor_key,
            address.connection,
        )
        return []

    cursor = last_seen(cursor_key, input_address)
    folders = sorted(e.name for e in response.entries if e.kind == "dir")
    unseen = folders if cursor is None else [n for n in folders if n > cursor]
    _touch_checked(cursor_key, input_address)
    return unseen


def scan_for_new_batches(detail: PipelineConfigDetail) -> list[str]:
    """Return the unseen batch folders under a pipeline's input (see :func:`scan_location`)."""
    return scan_location(detail.name, detail.input_address)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web worker; it is not a script.")
