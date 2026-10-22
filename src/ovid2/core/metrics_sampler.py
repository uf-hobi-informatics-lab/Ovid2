"""Live engine-metrics sampler.

Runs a daemon thread that polls ``engine.get_metrics()`` on a fixed
cadence and emits each sample onto the bus as an ``engine.metrics``
event. The web tier tails the resulting JSONL over SSE so the run
detail page can show a live pulse.

Design notes
------------

- **Free function, not a context manager.** The caller
  (``ovid2 etl`` / ``ovid2 tools copy`` / ``ovid2 tools move``) creates
  a ``threading.Event`` for shutdown, passes it in with the engine +
  bus + instance id, and stops the sampler when the run finishes. This
  keeps ``OvidInstance``'s constructor signature stable and matches
  where the engine reference already lives at the call site.
- **Daemon thread.** Never blocks pipeline shutdown; if the pipeline
  crashes hard the thread dies with it.
- **Adaptive rate.** Samples at ``spark_ms`` (500 ms) or
  ``snowpark_ms`` (5 s) while active; after
  ``ENGINE_METRICS_IDLE_AFTER`` consecutive samples with zero active
  tasks it backs off to ``idle_ms``. First non-zero sample restores
  the active rate.
- **Exception-safe.** Any exception in ``get_metrics()`` is caught and
  logged at debug — the sampler must never fail the run.
- **Never triggers a job.** ``get_metrics()`` uses metadata-only calls
  (``StatusTracker`` on Spark; nothing at all on Snowpark phase 1).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from ovid2.core.events import EventBus, engine_metrics

if TYPE_CHECKING:
    from ovid2.core.engines.base import BaseEngine

logger = logging.getLogger(__name__)


class SamplerHandle:
    """Handle returned by :func:`run_sampler`.

    Wraps the stop event and the daemon thread. The caller invokes
    :meth:`stop` in a ``finally`` block; the thread joins with a short
    timeout so pipeline shutdown never hangs on a stuck sampler.
    """

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self._thread = thread
        self._stop_event = stop_event

    def stop(self, *, timeout: float = 1.0) -> None:
        """Signal the sampler to stop and join briefly."""
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()


def run_sampler(
    *,
    instance_id: str,
    engine: "BaseEngine",
    bus: EventBus,
    active_ms: int | None = None,
    idle_ms: int | None = None,
    idle_after: int | None = None,
) -> SamplerHandle:
    """Start a metrics-sampling daemon thread.

    Args:
        instance_id: The run's instance id. Every emitted event carries
            it so consumers can join back to the run's event log.
        engine: The active :class:`BaseEngine`. The sampler holds a
            weak-ish reference — if the engine tears down its session,
            :meth:`get_metrics` still returns a bare "alive" sample.
        bus: Where to emit samples. Typically the same
            ``JsonlEventBus`` the pipeline uses.
        active_ms: Sampling period while tasks are running. Defaults
            to :data:`ENGINE_METRICS_SPARK_MS` for Spark family and
            :data:`ENGINE_METRICS_SNOWPARK_MS` for Snowpark. ``0``
            disables sampling (returns a stopped handle).
        idle_ms: Sampling period once
            :data:`ENGINE_METRICS_IDLE_AFTER` consecutive samples had
            zero active tasks.
        idle_after: Number of zero-active samples that trigger the
            switch to ``idle_ms``.

    Returns:
        A :class:`SamplerHandle`. The caller MUST call
        :meth:`SamplerHandle.stop` in a ``finally`` block.

    The returned handle is safe to :meth:`stop` from any thread and
    idempotent — a second call is a no-op.
    """
    # Local imports so this module stays cheap to import from the web
    # tier's test-collection path.
    from ovid2.config.settings import (
        ENGINE_METRICS_ENABLED,
        ENGINE_METRICS_IDLE_AFTER,
        ENGINE_METRICS_IDLE_MS,
        ENGINE_METRICS_SNOWPARK_MS,
        ENGINE_METRICS_SPARK_MS,
    )
    from ovid2.core.engines.protocols import EngineFamily

    family_value = (
        engine.family.value
        if isinstance(engine.family, EngineFamily)
        else str(engine.family)
    )

    if active_ms is None:
        active_ms = (
            ENGINE_METRICS_SPARK_MS
            if family_value != "snowpark"
            else ENGINE_METRICS_SNOWPARK_MS
        )
    if idle_ms is None:
        idle_ms = ENGINE_METRICS_IDLE_MS
    if idle_after is None:
        idle_after = ENGINE_METRICS_IDLE_AFTER

    stop_event = threading.Event()

    if not ENGINE_METRICS_ENABLED or active_ms <= 0:
        # Return a stopped handle so callers can uniformly ``.stop()``.
        stop_event.set()
        dummy = threading.Thread(target=lambda: None, daemon=True)
        dummy.start()
        return SamplerHandle(dummy, stop_event)

    def _run() -> None:
        """Sampler loop — cheap, resilient, adaptive."""
        zero_active_streak = 0
        while not stop_event.is_set():
            try:
                sample = engine.get_metrics()
                payload = sample.to_payload()
                bus.emit(engine_metrics(instance_id=instance_id, sample=payload))

                tasks_active = int(payload.get("tasks_active") or 0)
                if tasks_active == 0:
                    zero_active_streak += 1
                else:
                    zero_active_streak = 0
            except Exception as exc:  # noqa: BLE001
                # Metrics failure must never fail the run.
                logger.debug("engine metrics sample failed: %s", exc)
                zero_active_streak += 1

            # Choose the next wait period based on activity. Convert
            # ms → seconds for ``Event.wait``; the wait is
            # interruptible so ``stop()`` returns promptly.
            period_ms = idle_ms if zero_active_streak >= idle_after else active_ms
            if stop_event.wait(timeout=max(period_ms, 50) / 1000.0):
                break

    thread = threading.Thread(
        target=_run,
        name=f"ovid2-metrics-sampler-{instance_id}",
        daemon=True,
    )
    thread.start()
    return SamplerHandle(thread, stop_event)


__all__ = ["SamplerHandle", "run_sampler"]
