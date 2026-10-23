"""Largest-first scheduling for ``ovid2 tools copy`` / ``move``.

Copy fans tables out over a bounded thread pool
(:data:`ovid2.tools.copy._DEFAULT_COPY_TABLE_CONCURRENCY`). The pool
starts tables in list order, so a large table near the end of the list
(``lab_result_cm`` sits 9th in the PCORnet order, ``obs_clin`` 14th)
started late and kept the run going while most workers idled.

Starting the largest tables first is the standard
longest-processing-time-first (LPT) heuristic. It is not guaranteed
optimal (the general problem is NP-hard), but it stays within
``4/3 - 1/(3m)`` of the optimal makespan for ``m`` parallel slots, and
for a typical CDM copy it makes the run as long as the single largest
table instead of "largest table + however late it started".

Sizes come from :meth:`ovid2.drivers.base.Driver.table_sizes` — storage
metadata only (file ``stat``, object / blob listing, Snowflake ``SHOW
TABLES`` ``bytes``). No data scan, no Spark action, so the lazy-Spark
rule (AGENTS.md §1) is unaffected.

``$OVID2_COPY_ORDER`` controls the policy:

- ``largest_first`` (default) — sort by known size, descending; tables
  with unknown size keep their relative order *after* the sized ones.
- ``as_given`` — keep the operator's order (the pre-2026-10 behaviour).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ovid2.drivers.base import Driver
    from ovid2.tools.table_specs import TableSpec

logger = logging.getLogger(__name__)

#: Accepted values of ``$OVID2_COPY_ORDER``.
ORDER_LARGEST_FIRST = "largest_first"
ORDER_AS_GIVEN = "as_given"
_VALID_ORDERS: frozenset[str] = frozenset({ORDER_LARGEST_FIRST, ORDER_AS_GIVEN})


def copy_order_policy() -> str:
    """Return the active ordering policy from ``$OVID2_COPY_ORDER``.

    Unknown values log a warning and fall back to ``largest_first``.
    """
    raw = (os.environ.get("OVID2_COPY_ORDER") or ORDER_LARGEST_FIRST).strip().lower()
    if raw not in _VALID_ORDERS:
        logger.warning(
            "Ignoring unknown OVID2_COPY_ORDER=%r; using %s.",
            raw,
            ORDER_LARGEST_FIRST,
        )
        return ORDER_LARGEST_FIRST
    return raw


def sort_by_size(
    specs: list[TableSpec],
    sizes: dict[str, int],
) -> list[TableSpec]:
    """Return ``specs`` sorted largest-first by ``sizes[spec.file_name]``.

    Stable: ties and unknown-size specs keep their input order; unknown
    sizes sort after every known size.
    """
    indexed = list(enumerate(specs))
    indexed.sort(
        key=lambda pair: (
            0 if pair[1].file_name in sizes else 1,
            -sizes.get(pair[1].file_name, 0),
            pair[0],
        )
    )
    return [spec for _, spec in indexed]


def order_largest_first(
    specs: list[TableSpec],
    driver: Driver,
    *,
    pipeline: str,
    batch_id: str,
) -> tuple[list[TableSpec], dict[str, int]]:
    """Reorder ``specs`` so the largest source tables start first.

    Best-effort: any failure looking up sizes (SDK missing, permission
    denied, transient API error) is logged and the original order is
    kept — scheduling must never fail a copy.

    Args:
        specs: Parsed ``--tables`` entries.
        driver: The *source* driver.
        pipeline: Pipeline name.
        batch_id: Batch identifier.

    Returns:
        ``(ordered_specs, sizes)`` — ``sizes`` is the
        ``{file_name: bytes}`` map that drove the order (empty when the
        policy is ``as_given`` or no size was known).
    """
    if len(specs) < 2 or copy_order_policy() == ORDER_AS_GIVEN:
        return list(specs), {}
    try:
        sizes = driver.table_sizes(
            [s.file_name for s in specs],
            pipeline=pipeline,
            batch_id=batch_id,
        )
    except Exception as exc:  # scheduling is advisory; never fail the copy
        logger.warning(
            "Could not read table sizes from %s; keeping the given order: %s",
            getattr(driver, "type_name", "?"),
            exc,
        )
        return list(specs), {}
    if not isinstance(sizes, dict) or not sizes:
        return list(specs), {}
    sizes = {k: v for k, v in sizes.items() if isinstance(v, int) and v >= 0}
    if not sizes:
        return list(specs), {}
    return sort_by_size(specs, sizes), sizes


def format_size(size: int) -> str:
    """Return a short human-readable byte size (``1.2 GB``)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - loop always returns


__all__ = [
    "ORDER_AS_GIVEN",
    "ORDER_LARGEST_FIRST",
    "copy_order_policy",
    "format_size",
    "order_largest_first",
    "sort_by_size",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by ovid2 tools copy/move; it is not a script.")
