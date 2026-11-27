"""Process-wide cooperative-cancel flag for a run.

The ETL entry point's SIGTERM / SIGINT handler raises ``KeyboardInterrupt``
so the run unwinds. Engines do not always let that exception through:
DuckDB, for one, converts a ``KeyboardInterrupt`` raised while it streams a
result into ``RuntimeError("Query interrupted")``. A table loop that treats
every ``Exception`` as "this table failed, try the next one" would then
keep starting tables after the operator cancelled.

The handler therefore also calls :func:`request_cancel`; table loops call
:func:`raise_if_cancelled` before each table and after a table failure,
which turns any exception seen after a cancel back into
``KeyboardInterrupt``.
"""

from __future__ import annotations

import threading

_cancelled = threading.Event()


def request_cancel() -> None:
    """Mark the current run as cancelled (idempotent, signal-safe)."""
    _cancelled.set()


def is_cancelled() -> bool:
    """Return ``True`` once :func:`request_cancel` has been called."""
    return _cancelled.is_set()


def raise_if_cancelled(cause: BaseException | None = None) -> None:
    """Raise ``KeyboardInterrupt`` when the run was cancelled.

    Args:
        cause: The exception being handled, chained as ``__cause__``.

    Raises:
        KeyboardInterrupt: When :func:`request_cancel` has been called.
    """
    if _cancelled.is_set():
        raise KeyboardInterrupt("run cancelled") from cause


def reset() -> None:
    """Clear the flag (tests, or a process that runs more than one job)."""
    _cancelled.clear()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.core.cancellation is a library module; run `ovid2 etl`.")
