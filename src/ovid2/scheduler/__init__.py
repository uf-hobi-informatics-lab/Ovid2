"""Batch scheduler.

The scheduler polls pipeline inboxes, dispatches ETL runs in parallel
slots, each of which spawns a fresh
:class:`~ovid2.core.isolation.OvidInstance` with its own scratch
resources. Batch state is persisted in Snowflake ``OVID.SCHEDULER`` or a
local SQLite fallback via the abstractions in
:mod:`ovid2.scheduler.batch_tracker`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.scheduler.batch_tracker import (
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    BatchTracker,
    SnowflakeBatchTracker,
    SqliteBatchTracker,
)

if TYPE_CHECKING:  # pragma: no cover
    from ovid2.scheduler.service import SchedulerService


def __getattr__(name: str) -> Any:
    """Load :class:`SchedulerService` lazily.

    ``scheduler.service`` pulls in ``ovid2.core.events`` and
    ``ovid2.core.isolation``. The web tier imports only
    :mod:`ovid2.scheduler.batch_tracker`, so the service module must not
    be loaded as a side effect of importing this package (enforced by
    ``tests/contract/test_tier_import_closure.py``).
    """
    if name == "SchedulerService":
        from ovid2.scheduler.service import SchedulerService as _SchedulerService

        return _SchedulerService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "STATUS_ERROR",
    "STATUS_NEW",
    "STATUS_PROCESSED",
    "STATUS_PROCESSING",
    "BatchTracker",
    "SchedulerService",
    "SnowflakeBatchTracker",
    "SqliteBatchTracker",
]
