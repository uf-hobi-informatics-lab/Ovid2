"""Batch scheduler.

The scheduler polls pipeline inboxes, dispatches ETL runs in parallel
slots, each of which spawns a fresh
:class:`~ovid2.core.isolation.OvidInstance` with its own scratch
resources. Batch state is persisted in Snowflake ``OVID.SCHEDULER`` or a
local SQLite fallback via the abstractions in
:mod:`ovid2.scheduler.batch_tracker`.
"""

from __future__ import annotations

from ovid2.scheduler.batch_tracker import (
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    BatchTracker,
    SnowflakeBatchTracker,
    SqliteBatchTracker,
)
from ovid2.scheduler.service import SchedulerService

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
