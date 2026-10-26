"""Address-is-exact-location helpers for the web tier.

Two jobs, both web-safe (no secret is ever resolved here):

- :func:`batch_id_for_address` — derive a run's ``batch_id`` from its
  input address. The batch id is the *complete* Data Space path of the
  input location: the storage account (connection) followed by the
  folder path or ``DATABASE/SCHEMA``. It is a label; drivers never
  append it to an address.
- :func:`existing_output_tables` — the overwrite pre-flight. Brokered
  through ``ovid2 tools probe-output`` so the API process never holds a
  credential (``test_web_no_plaintext_secret_reads.py``).

See the adr-address-is-exact-location design note.
"""

from __future__ import annotations

import logging

from ovid2.drivers._layout import dataspace_path, location_path
from ovid2.web.services import addresses_svc

logger = logging.getLogger(__name__)

__all__ = ["OutputProbe", "batch_id_for_address", "existing_output_tables"]


def batch_id_for_address(address_name: str) -> str | None:
    """Return the Data Space path ``<connection>/<location path>`` of an address.

    Returns ``None`` for unknown or legacy (connectionless) addresses —
    the caller then falls back to the operator-supplied label.
    """
    if not (address_name or "").strip():
        return None
    detail = addresses_svc.get_address(address_name)
    if detail is None or not detail.connection:
        return None
    warehouse = addresses_svc.is_warehouse_connection(detail.connection)
    try:
        return dataspace_path(
            detail.connection, location_path(detail.location or {}, warehouse=warehouse)
        )
    except ValueError as exc:
        logger.warning("cannot derive batch id from address %r: %s", address_name, exc)
        return None


class OutputProbe:
    """Outcome of the overwrite pre-flight.

    Attributes:
        known: ``False`` when occupancy could not be determined (the
            executor still enforces its own gate at run time).
        tables: The tables already present (empty when none / unknown).
        message: Human-readable detail when ``known`` is ``False``.
    """

    __slots__ = ("known", "message", "tables")

    def __init__(self, *, known: bool, tables: list[str], message: str = "") -> None:
        self.known = known
        self.tables = tables
        self.message = message

    @property
    def occupied(self) -> bool:
        """``True`` when the output is known to already hold tables."""
        return self.known and bool(self.tables)


def existing_output_tables(address_name: str) -> OutputProbe:
    """Return the tables already at ``address_name``'s exact location.

    Never raises; broker failures map to ``known=False``.
    """
    from ovid2.web.services._broker import BrokerError, run_tool_json
    from ovid2.web.services.storage_browse_svc import _browse_deadline_seconds

    if not (address_name or "").strip():
        return OutputProbe(known=False, tables=[], message="no output address")
    try:
        result = run_tool_json(["probe-output", address_name], timeout=_browse_deadline_seconds())
    except BrokerError as exc:
        logger.warning("probe-output for '%s' failed: %s", address_name, exc)
        return OutputProbe(known=False, tables=[], message=str(exc))
    if not result.get("ok") or not result.get("known"):
        return OutputProbe(known=False, tables=[], message=str(result.get("message") or ""))
    tables = [str(t) for t in (result.get("tables") or []) if str(t).strip()]
    return OutputProbe(known=True, tables=tables)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
