"""Overwrite gate — a run never silently overwrites an occupied output.

The address is the exact location a run writes to (see
the adr-address-is-exact-location design note). Since no
``{pipeline}/{batch_id}`` sub-folder isolates runs any more, writing into
an output that already holds tables replaces that data. That requires an
explicit confirmation:

- CLI / executor: ``--overwrite`` on ``ovid2 etl | analysis | health |
  dq_checks | tools copy | tools move``;
- web: ``confirm_overwrite`` on the submit / run / rerun / launch surfaces
  (the API answers ``409 overwrite_required`` until it is sent);
- agent (MCP / assistant): the ``run.overwrite`` human-approval gate.

This module is the executor-side backstop shared by every CLI entry
point. It is import-safe (the driver registry is imported lazily).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["OutputNotEmptyError", "assert_output_writable", "probe_output_tables"]


class OutputNotEmptyError(RuntimeError):
    """Raised when an output location already holds tables and no overwrite was confirmed.

    Attributes:
        address: The output address name (or a description of it).
        tables: The tables already present at the location.
    """

    def __init__(self, address: str, tables: list[str]) -> None:
        self.address = address
        self.tables = list(tables)
        preview = ", ".join(self.tables[:10]) + (" …" if len(self.tables) > 10 else "")
        super().__init__(
            f"Output '{address}' already contains {len(self.tables)} table(s) "
            f"({preview}). The address is the exact write location, so this run "
            "would overwrite them. Re-run with --overwrite (web: confirm the "
            "overwrite) to replace the existing data."
        )


def probe_output_tables(driver: Any) -> list[str] | None:
    """Return the tables already at ``driver``'s location, or ``None`` if unknown.

    Never raises: any listing failure (missing SDK, credentials, bad
    location) is logged and reported as *unknown*.
    """
    try:
        result = driver.existing_tables()
    except Exception as exc:
        logger.warning(
            "overwrite gate: could not list output %s: %s",
            getattr(driver, "type_name", "?"),
            exc,
        )
        return None
    return None if result is None else [str(t) for t in result]


def assert_output_writable(
    address_name: str | None,
    *,
    overwrite: bool,
    driver: Any | None = None,
    pipeline: str | None = None,
    only_tables: list[str] | None = None,
) -> list[str] | None:
    """Fail fast when the output location is occupied and overwrite is not confirmed.

    Args:
        address_name: Output address config name (used to build the
            driver when ``driver`` is not given, and in messages).
        overwrite: ``True`` when the operator confirmed the overwrite.
        driver: An already-resolved output driver (optional).
        pipeline: Pipeline label (Snowflake per-pipeline defaults).
        only_tables: When given, only these table names (case-insensitive)
            count as "would be overwritten" — used by runs that write a
            single known table into a shared location (``health`` writes
            ``dcq_results`` next to the CDM tables it reads).

    Returns:
        The tables found at the output (``[]`` when empty, ``None`` when
        occupancy could not be determined).

    Raises:
        OutputNotEmptyError: When the output holds tables and
            ``overwrite`` is ``False``.
    """
    label = address_name or "<output>"
    if driver is None:
        if not address_name:
            return None
        try:
            from ovid2.drivers.registry import driver_from_address

            driver = driver_from_address(address_name, pipeline=pipeline)
        except Exception as exc:
            logger.warning("overwrite gate: cannot resolve output '%s': %s", label, exc)
            return None
    tables = probe_output_tables(driver)
    if tables is None:
        logger.warning(
            "overwrite gate: output '%s' cannot report its tables; proceeding without "
            "the occupancy check.",
            label,
        )
        return None
    if only_tables is not None:
        wanted = {t.lower() for t in only_tables}
        tables = [t for t in tables if t.lower() in wanted]
    if tables and not overwrite:
        raise OutputNotEmptyError(label, tables)
    if tables:
        logger.warning(
            "overwrite gate: output '%s' holds %d table(s); overwrite confirmed.",
            label,
            len(tables),
        )
    return tables


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
