"""Engine-workspace helpers shared by the executor CLIs (``etl`` / ``health``).

Compute engines that create scratch objects inside a warehouse (Snowpark:
temp views, transient staging tables) need a writable database. When the
engine's own configuration names none, the run's warehouse addresses are
the natural candidates — the operator's role already reads / writes there.
This module derives those candidates, output first, through the driver
metadata hook :meth:`ovid2.drivers.base.Driver.workspace_database`, so the
CLIs never branch on a driver type.

The derived names are only ever used for engine scratch objects, never as
a write target: the "an address is the exact location" rule is unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


def workspace_databases_from_drivers(drivers: Iterable[Driver | None]) -> list[str]:
    """Return the distinct workspace databases of ``drivers``, in order.

    Args:
        drivers: Resolved drivers (``None`` entries are skipped), most
            preferred first — pass the output before the inputs.

    Returns:
        Database names, de-duplicated case-insensitively.
    """
    out: list[str] = []
    seen: set[str] = set()
    for driver in drivers:
        if driver is None:
            continue
        try:
            name = type(driver).workspace_database(getattr(driver, "location", None))
        except Exception as exc:
            logger.debug("workspace_database failed for %r: %s", driver, exc)
            continue
        if name and name.upper() not in seen:
            seen.add(name.upper())
            out.append(name)
    return out


def workspace_databases_from_addresses(
    address_names: Iterable[str | None],
    *,
    pipeline: str | None = None,
    sf_overrides: dict[str, Any] | None = None,
) -> list[str]:
    """Return workspace-database candidates for saved address names.

    Each address is resolved with
    :func:`ovid2.drivers.registry.driver_from_address` (which applies the
    Snowflake four-tier database resolution, including the per-pipeline
    default). An address that fails to resolve is skipped — this is a
    best-effort fallback, the run's own address loading reports real
    errors.

    Args:
        address_names: Address names, most preferred first (output, then
            inputs). ``None`` / empty entries are skipped.
        pipeline: Pipeline label (Snowflake per-pipeline default database).
        sf_overrides: CLI Snowflake overrides (``--sf_database`` …).

    Returns:
        Distinct database names, in preference order.
    """
    from ovid2.drivers.registry import driver_from_address

    drivers: list[Driver | None] = []
    for name in address_names:
        if not name:
            continue
        try:
            drivers.append(driver_from_address(name, pipeline=pipeline, sf_overrides=sf_overrides))
        except Exception as exc:
            logger.debug("workspace: address %s not resolvable: %s", name, exc)
    return workspace_databases_from_drivers(drivers)


if __name__ == "__main__":
    raise SystemExit("ovid2.cli._workspace is a library module.")
