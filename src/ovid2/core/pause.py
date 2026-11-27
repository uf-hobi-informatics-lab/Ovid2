"""Cooperative pause at a table boundary (executor side).

The web worker pauses a running job by dropping a *pause flag* next to the
run's event log (:mod:`ovid2.config.run_control`). ``WORK_DIR`` is
bind-mounted at the identical absolute path into every driver container, so
the in-container ``ovid2 etl`` / ``ovid2 tools copy|move`` sees the flag
without any extra mount.

Table loops call :func:`wait_if_paused` *between* tables, never inside one:
the table that is running finishes (its lazy plan is already executing at
the write), then the loop holds until the flag is removed (resume) or the
run is cancelled. :func:`run_pause_hooks` builds the callbacks that emit a
run-level ``stage.progress`` (``stage="paused"``, ``extra.state``
``waiting`` / ``active``) so the run page and the Atlas show the pause and
freeze the clocks.

Lazy-safe: it never touches a DataFrame.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ovid2.config.run_control import is_paused
from ovid2.core.cancellation import raise_if_cancelled

#: Seconds between polls of the pause flag while paused.
PAUSE_POLL_SECONDS: float = 1.0


def wait_if_paused(
    instance_id: str,
    *,
    on_pause: Callable[[], None] | None = None,
    on_resume: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block while the run is paused; return ``True`` if it was paused.

    A cancel while paused raises ``KeyboardInterrupt`` (through
    :func:`ovid2.core.cancellation.raise_if_cancelled`), so a paused run is
    always cancellable.

    Args:
        instance_id: The run's instance id (``""`` → never paused).
        on_pause: Called once when the hold starts (emit the event).
        on_resume: Called once when the hold ends normally.
        sleep: Injected for tests.
    """
    if not is_paused(instance_id):
        return False
    if on_pause is not None:
        on_pause()
    while is_paused(instance_id):
        raise_if_cancelled()
        sleep(PAUSE_POLL_SECONDS)
    raise_if_cancelled()
    if on_resume is not None:
        on_resume()
    return True


def run_pause_hooks(
    bus: Any, *, instance_id: str, pipeline: str, batch_id: str
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Return ``(on_pause, on_resume)`` callbacks that emit the run-level events."""
    from ovid2.core.events import run_paused, run_resumed

    def _pause() -> None:
        bus.emit(run_paused(instance_id=instance_id, pipeline=pipeline, batch_id=batch_id))

    def _resume() -> None:
        bus.emit(run_resumed(instance_id=instance_id, pipeline=pipeline, batch_id=batch_id))

    return _pause, _resume


def gate(bus: Any, *, instance_id: str, pipeline: str, batch_id: str) -> Callable[[], None]:
    """Return a zero-arg table-boundary gate for a table loop."""
    on_pause, on_resume = run_pause_hooks(
        bus, instance_id=instance_id, pipeline=pipeline, batch_id=batch_id
    )

    def _gate() -> None:
        wait_if_paused(instance_id, on_pause=on_pause, on_resume=on_resume)

    return _gate


__all__ = ["PAUSE_POLL_SECONDS", "gate", "run_pause_hooks", "wait_if_paused"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.core.pause is a library module.")
