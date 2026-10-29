"""Ovid2 event definitions.

Every runtime signal from the pipeline flows through a single **event bus**.
Events are plain dataclasses in Python, but on the wire they are one JSON
object per line — the format described in
``ovid2/resources/schemas/events.v1.json``.

Design notes:

- The pipeline emits typed events; it never imports :mod:`ovid2.display`
  (enforced by ``tests/contract/test_display_isolation.py``).
- ``EventBus`` is a :class:`Protocol` so any object with ``emit(event)``
  qualifies. The process-wide default is :class:`NullEventBus`; the CLI
  installs a :class:`~ovid2.display.bus.JsonlEventBus` at startup.
- The ``@event`` helper wraps the raw payload dict so callers construct
  events with correct fields; malformed events fail a contract test.

Schema version: **1**.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

# ``datetime.UTC`` is 3.11+; ``timezone.utc`` works everywhere we support.
_UTC = UTC

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """A single pipeline event.

    Attributes:
        kind: Dotted event name (see :data:`EVENT_KINDS`).
        payload: Event-specific fields; must match the schema for ``kind``.
        instance_id: Owning :class:`~ovid2.core.isolation.OvidInstance` id.
        timestamp: UTC timestamp when the event was constructed.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    instance_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(_UTC))

    def to_json_dict(self) -> dict[str, Any]:
        """Return a schema-shaped dict for JSONL serialisation."""
        record: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": self.kind,
            "ts": self.timestamp.astimezone(_UTC).isoformat(),
            "instance_id": self.instance_id,
        }
        # Merge payload last so it can override ``instance_id`` / ``ts`` only
        # in tests. Real callers should leave those envelope fields alone.
        for key, value in self.payload.items():
            if value is None:
                continue
            record[key] = value
        return record


EVENT_KINDS: frozenset[str] = frozenset(
    {
        "instance.started",
        "instance.finished",
        "batch.received",
        "batch.dispatched",
        "batch.completed",
        "batch.failed",
        "table.started",
        "table.completed",
        "table.failed",
        "check.started",
        "check.completed",
        "check.failed",
        "stage.progress",
        "fix.applied",
        "fix.skipped",
        "fix.failed",
        "engine.metrics",
        "log",
        "warning",
        "error",
    }
)


# ---------------------------------------------------------------------------
# Typed constructors — every call site should use one of these instead of
# building the payload dict inline, so the schema and Python stay in sync.
# ---------------------------------------------------------------------------


def instance_started(*, instance_id: str, pipeline: str | None = None) -> Event:
    """Emitted when an :class:`OvidInstance` begins execution."""
    payload: dict[str, Any] = {}
    if pipeline:
        payload["pipeline"] = pipeline
    return Event(kind="instance.started", payload=payload, instance_id=instance_id)


def instance_finished(*, instance_id: str, success: bool) -> Event:
    """Emitted when the process is about to exit."""
    return Event(
        kind="instance.finished",
        payload={"extra": {"success": success}},
        instance_id=instance_id,
    )


def table_started(*, instance_id: str, pipeline: str, batch_id: str, table: str) -> Event:
    return Event(
        kind="table.started",
        payload={"pipeline": pipeline, "batch_id": batch_id, "table": table},
        instance_id=instance_id,
    )


def table_completed(
    *, instance_id: str, pipeline: str, batch_id: str, table: str, elapsed_ms: float | None = None
) -> Event:
    payload: dict[str, Any] = {"pipeline": pipeline, "batch_id": batch_id, "table": table}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return Event(kind="table.completed", payload=payload, instance_id=instance_id)


def table_failed(
    *, instance_id: str, pipeline: str, batch_id: str, table: str, error: str
) -> Event:
    return Event(
        kind="table.failed",
        payload={"pipeline": pipeline, "batch_id": batch_id, "table": table, "error": error},
        instance_id=instance_id,
    )


def fix_applied(
    *,
    instance_id: str,
    pipeline: str,
    batch_id: str,
    table: str,
    fix: str,
    fix_type: str = "common",
    current_fix: int = 0,
    total_fixes: int = 0,
    src1: str = "",
    src2: str = "",
    file_name: str = "",
) -> Event:
    return Event(
        kind="fix.applied",
        payload={
            "pipeline": pipeline,
            "batch_id": batch_id,
            "table": table,
            "fix": fix,
            "fix_type": fix_type,
            "current_fix": current_fix,
            "total_fixes": total_fixes,
            "src1": src1,
            "src2": src2,
            "file_name": file_name,
        },
        instance_id=instance_id,
    )


def fix_failed(
    *,
    instance_id: str,
    pipeline: str,
    batch_id: str,
    table: str,
    fix: str,
    error: str,
    fix_type: str = "common",
    current_fix: int = 0,
    total_fixes: int = 0,
    src1: str = "",
    src2: str = "",
    file_name: str = "",
) -> Event:
    return Event(
        kind="fix.failed",
        payload={
            "pipeline": pipeline,
            "batch_id": batch_id,
            "table": table,
            "fix": fix,
            "error": error,
            "fix_type": fix_type,
            "current_fix": current_fix,
            "total_fixes": total_fixes,
            "src1": src1,
            "src2": src2,
            "file_name": file_name,
        },
        instance_id=instance_id,
    )


def check_started(*, instance_id: str, check_id: str, name: str, part: str) -> Event:
    """Emitted when a data-health check begins (``ovid2 health``)."""
    return Event(
        kind="check.started",
        payload={"check_id": check_id, "check_name": name, "part": part},
        instance_id=instance_id,
    )


def check_completed(
    *,
    instance_id: str,
    check_id: str,
    name: str,
    part: str,
    status: str,
    exception_count: int = 0,
    metric_count: int = 0,
    elapsed_ms: float | None = None,
) -> Event:
    """Emitted when a data-health check finishes (passed / exception)."""
    payload: dict[str, Any] = {
        "check_id": check_id,
        "check_name": name,
        "part": part,
        "check_status": status,
        "exception_count": exception_count,
        "metric_count": metric_count,
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return Event(
        kind="check.completed",
        payload=payload,
        instance_id=instance_id,
    )


def check_failed(
    *,
    instance_id: str,
    check_id: str,
    name: str,
    part: str,
    error: str,
    elapsed_ms: float | None = None,
) -> Event:
    """Emitted when a data-health check raises an error."""
    payload: dict[str, Any] = {
        "check_id": check_id,
        "check_name": name,
        "part": part,
        "error": error,
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return Event(
        kind="check.failed",
        payload=payload,
        instance_id=instance_id,
    )


def batch_completed(
    *, instance_id: str, pipeline: str, batch_id: str, elapsed_ms: float | None = None
) -> Event:
    payload: dict[str, Any] = {"pipeline": pipeline, "batch_id": batch_id}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return Event(kind="batch.completed", payload=payload, instance_id=instance_id)


def batch_failed(*, instance_id: str, pipeline: str, batch_id: str, error: str) -> Event:
    return Event(
        kind="batch.failed",
        payload={"pipeline": pipeline, "batch_id": batch_id, "error": error},
        instance_id=instance_id,
    )


def log_event(*, instance_id: str, level: str, message: str, **extra: Any) -> Event:
    """Emit a free-form log line. Prefer typed constructors when possible."""
    payload: dict[str, Any] = {"level": level, "message": message}
    if extra:
        payload["extra"] = extra
    return Event(kind="log", payload=payload, instance_id=instance_id)


def warning_event(*, instance_id: str, message: str, **extra: Any) -> Event:
    payload: dict[str, Any] = {"message": message}
    if extra:
        payload["extra"] = extra
    return Event(kind="warning", payload=payload, instance_id=instance_id)


def error_event(*, instance_id: str, message: str, **extra: Any) -> Event:
    payload: dict[str, Any] = {"message": message}
    if extra:
        payload["extra"] = extra
    return Event(kind="error", payload=payload, instance_id=instance_id)


def engine_metrics(*, instance_id: str, sample: dict[str, Any]) -> Event:
    """Emit a live engine-metrics sample.

    ``sample`` is a plain dict shaped like
    :class:`ovid2.core.engines.metrics.EngineMetricsSample` — kept as a
    dict rather than importing the dataclass so this module stays
    free of any engine dependency (display isolation).

    Every field is optional: engines that cannot report a field simply
    leave it out. The frontend renders whatever's present.
    """
    return Event(kind="engine.metrics", payload=dict(sample), instance_id=instance_id)


# ---------------------------------------------------------------------------
# EventBus protocol + default null bus
# ---------------------------------------------------------------------------


class EventBus(Protocol):
    """Any object that can accept events."""

    def emit(self, event: Event) -> None:
        """Publish an event."""


class NullEventBus:
    """Default bus that logs at DEBUG and drops every event.

    Used when no CLI bootstrap has installed a real bus (unit tests, imports).
    """

    def emit(self, event: Event) -> None:
        logger.debug("event %s payload=%s", event.kind, event.payload)


_default_bus: EventBus = NullEventBus()


def get_default_bus() -> EventBus:
    """Return the process-wide default event bus."""
    return _default_bus


def set_default_bus(bus: EventBus) -> None:
    """Install ``bus`` as the process-wide default.

    Intended for tests and the CLI entry point.
    """
    global _default_bus
    _default_bus = bus


__all__ = [
    "EVENT_KINDS",
    "SCHEMA_VERSION",
    "Event",
    "EventBus",
    "NullEventBus",
    "asdict",
    "batch_completed",
    "batch_failed",
    "check_completed",
    "check_failed",
    "check_started",
    "engine_metrics",
    "error_event",
    "fix_applied",
    "fix_failed",
    "get_default_bus",
    "instance_finished",
    "instance_started",
    "log_event",
    "set_default_bus",
    "table_completed",
    "table_failed",
    "table_started",
    "warning_event",
]
