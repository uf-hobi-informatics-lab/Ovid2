"""Run-control flags shared by the web worker and the executor (web-safe).

A *pause* is requested by the trusted web tier and honoured by the executor
at the next table boundary (:mod:`ovid2.core.pause`). The two sides never
talk directly: the worker drops a flag file next to the run's event log —
``{WORK_DIR}/pause-{instance_id}`` — and removes it to resume. ``WORK_DIR`` is
bind-mounted at the identical absolute path into every driver container, so
the same path is valid on both sides.

This module is stdlib-only so the web tier may import it
(``tests/contract/test_tier_import_closure.py``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: File-name prefix of the pause flag in ``WORK_DIR``.
PAUSE_FLAG_PREFIX: str = "pause-"

_INSTANCE_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def _work_dir() -> Path:
    """Return the current ``WORK_DIR`` (late import: env may change in tests)."""
    from ovid2.config.settings import WORK_DIR

    return Path(os.environ.get("OVID2_WORK_DIR") or WORK_DIR)


def pause_flag_path(instance_id: str) -> Path:
    """Return the pause flag path of ``instance_id``.

    Args:
        instance_id: The run's ``YYYYMMDD-HHMMSS-<6hex>`` id.

    Raises:
        ValueError: When ``instance_id`` is not a valid instance id (the
            name lands in a path; never build one from free text).
    """
    if not _INSTANCE_RE.match(instance_id or ""):
        raise ValueError(f"not an instance id: {instance_id!r}")
    return _work_dir() / f"{PAUSE_FLAG_PREFIX}{instance_id}"


def is_paused(instance_id: str) -> bool:
    """Return ``True`` while the pause flag of ``instance_id`` exists."""
    try:
        return pause_flag_path(instance_id).exists()
    except ValueError:
        return False


def request_pause(instance_id: str) -> Path:
    """Create the pause flag (idempotent, mode 0600); return its path."""
    path = pause_flag_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    return path


def clear_pause(instance_id: str) -> bool:
    """Remove the pause flag; return ``True`` when one existed."""
    try:
        path = pause_flag_path(instance_id)
    except ValueError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "PAUSE_FLAG_PREFIX",
    "clear_pause",
    "is_paused",
    "pause_flag_path",
    "request_pause",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.run_control is a library module.")
