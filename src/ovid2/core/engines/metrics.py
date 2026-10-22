"""Live engine-metrics sample dataclass.

The pipeline's metrics sampler polls the active engine every N ms and
emits an ``engine.metrics`` event carrying one of these on the payload.

Design notes
------------

- Every field is ``Optional``: different engines expose different subsets
  of information. The frontend renders whatever is present and hides the
  rest.
- The sample is designed to be **cheap** to construct. On Spark this is
  a handful of in-JVM ``StatusTracker`` calls (metadata only, no jobs
  triggered). On Snowpark the phase-1 implementation returns only
  ``engine_family`` — no Snowflake round-trip.
- ``t_ms`` is the sampler's own monotonic clock reading; the client uses
  it to interpolate values between two consecutive samples. ``wall_iso``
  is the process wall clock for display / audit only.
- ``custom`` is the engine-specific escape hatch. Add fields there while
  we're prototyping; promote them to first-class attributes once we know
  the shape is stable.

This module intentionally imports nothing from PySpark / Snowpark so it
stays cheap to import from web-tier code too (metrics_svc parses the
JSONL back into a dict — no dataclass instantiation on the read path).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EngineMetricsSample:
    """One point-in-time snapshot of engine liveness.

    Attributes:
        t_ms: Monotonic clock reading (``time.monotonic() * 1000``) at
            the moment the sample was taken. Used by the client to
            interpolate between two consecutive samples smoothly.
        wall_iso: ISO-8601 wall-clock timestamp of the same moment.
        engine_family: ``"spark"`` or ``"snowpark"``. Never omitted.
        executors_active: How many executors currently have running
            tasks. ``None`` when the engine does not expose it.
        executors_total: Total executors alive (busy + idle).
        tasks_active: Tasks currently running across all active stages.
        tasks_completed_total: Cumulative task completions since job
            start.
        tasks_failed_total: Cumulative task failures since job start.
            The frontend watches this delta to enter the ``retrying``
            state — a climbing value with no ``table.failed`` event is
            a transient retry.
        stages_completed_total: Cumulative stage completions. Used for
            the stages-based ETA (``elapsed / done * (total - done)``).
        stages_total: Total stages seen so far (running + queued + done).
        current_stage: Short human-readable name of the newest active
            stage (e.g. ``"csv at DiscDriver.write:118"``). Empty string
            when nothing is running.
        input_bytes_total: Cumulative input bytes read across all stages.
        output_bytes_total: Cumulative output bytes written across all
            stages.
        rows_written_total: Cumulative rows written to output. Only
            populated by engines that hook a ``SparkListener`` that
            accumulates ``taskEnd.taskMetrics.outputMetrics.recordsWritten``.
        custom: Engine-specific extra fields (application driver URL,
            snowflake query ids, cluster name, …).
    """

    t_ms: int
    wall_iso: str
    engine_family: str

    executors_active: int | None = None
    executors_total: int | None = None
    tasks_active: int | None = None
    tasks_completed_total: int | None = None
    tasks_failed_total: int | None = None
    stages_completed_total: int | None = None
    stages_total: int | None = None
    current_stage: str | None = None
    input_bytes_total: int | None = None
    output_bytes_total: int | None = None
    rows_written_total: int | None = None

    custom: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict for the event bus.

        Fields that are ``None`` are dropped so the serialised event
        stays compact.
        """
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None and v != {}}


__all__ = ["EngineMetricsSample"]
