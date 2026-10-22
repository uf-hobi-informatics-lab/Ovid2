"""Persistent JSONL renderer.

Copies events from an instance's live event log (in ``$XDG_RUNTIME_DIR``)
into a durable location such as ``/var/log/ovid2/YYYY-MM-DD.jsonl``. This
gives operators a searchable history that survives process exit and
runtime-dir cleanup.

Two modes:

- **Copy-through** (:class:`JsonlFileBus`): in-process fan-out — every
  event the pipeline emits is written to both the runtime log and this
  archive file.
- **Import** (:func:`archive_event_log`): one-shot copy of a completed
  run's log into the archive. Useful when the pipeline forgot to attach
  the renderer or on already-finished instances.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import date
from pathlib import Path

from ovid2.core.events import Event

logger = logging.getLogger(__name__)


def _default_archive_dir() -> Path:
    """Return the archive dir: env override → ``/var/log/ovid2`` → XDG state."""
    override = os.environ.get("OVID2_ARCHIVE_DIR")
    if override:
        return Path(override)
    system = Path("/var/log/ovid2")
    if system.is_dir() and os.access(system, os.W_OK):
        return system
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg_state) / "ovid2" / "archive"


def _archive_path_for(archive_dir: Path, day: date | None = None) -> Path:
    """Return the daily archive file (``YYYY-MM-DD.jsonl``)."""
    day = day or date.today()
    return archive_dir / f"{day.isoformat()}.jsonl"


class JsonlFileBus:
    """Persistent-archive bus that appends every event to a daily file.

    Attributes:
        archive_dir: Directory holding ``YYYY-MM-DD.jsonl`` files.
    """

    def __init__(self, archive_dir: Path | None = None) -> None:
        self.archive_dir = archive_dir or _default_archive_dir()
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_day: date | None = None
        self._fh: object | None = None
        self._current_path: Path | None = None

    def emit(self, event: Event) -> None:
        """Append ``event`` to today's archive file."""
        today = event.timestamp.date()
        line = json.dumps(event.to_json_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            if self._current_day != today:
                self._rotate(today)
            assert self._fh is not None
            self._fh.write(line)  # type: ignore[attr-defined]
            self._fh.flush()  # type: ignore[attr-defined]

    def _rotate(self, day: date) -> None:
        """Close the previous handle and open today's archive file."""
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()  # type: ignore[attr-defined]
        self._current_path = _archive_path_for(self.archive_dir, day)
        self._current_day = day
        self._fh = self._current_path.open("a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        """Close the current archive handle."""
        with self._lock:
            if self._fh is not None:
                with contextlib.suppress(OSError):
                    self._fh.close()  # type: ignore[attr-defined]
                self._fh = None


def archive_event_log(source_log: Path, archive_dir: Path | None = None) -> Path:
    """Copy every event in ``source_log`` into the daily archive file.

    Args:
        source_log: An instance's JSONL event log.
        archive_dir: Destination directory. Defaults to
            :func:`_default_archive_dir`.

    Returns:
        The archive file path that received the events.
    """
    if not source_log.is_file():
        raise FileNotFoundError(f"Event log not found: {source_log}")
    dest_dir = archive_dir or _default_archive_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _archive_path_for(dest_dir)

    with source_log.open(encoding="utf-8") as src, dest.open("a", encoding="utf-8") as dst:
        for line in src:
            if line.strip():
                dst.write(line if line.endswith("\n") else line + "\n")
    return dest
