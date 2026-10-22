"""JSONL append-only event bus.

Serialises every :class:`ovid2.core.events.Event` as a single JSON object
on its own line to :attr:`OvidInstance.event_log`. Multiple processes can
tail the same file safely: writes are line-buffered and flushed after
every emit so consumers see events immediately.

Concurrency:
    A single :class:`JsonlEventBus` instance is safe to share across
    threads (a :class:`threading.Lock` guards the write path). Across
    processes, POSIX ``O_APPEND`` semantics guarantee that ``write()``
    calls smaller than ``PIPE_BUF`` (typically 4 KiB) are atomic — every
    event we emit is a single line under 4 KiB, so lines never
    interleave.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from ovid2.core.events import Event, EventBus

logger = logging.getLogger(__name__)


class JsonlEventBus:
    """Event bus that appends one JSON line per event to a file.

    Attributes:
        path: The event-log file (``OvidInstance.event_log`` in normal use).
    """

    def __init__(self, path: Path, *, sync: bool = False) -> None:
        """Open the log file for append.

        Args:
            path: Destination JSONL file. Parent directories are created
                automatically. The file itself is opened with mode ``0o600``
                so no other user can read pipeline internals.
            sync: When ``True``, ``fsync`` after every write. Off by default
                because most consumers only need line-flush semantics; on
                for the systemd deployment where a hard reset must not lose
                the final events.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create-or-append; ensure file mode is 0o600 the first time.
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        else:
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                logger.debug("Could not chmod %s: %s", self.path, exc)

        self._sync = bool(sync)
        # Line-buffered text mode. Every ``write("...\n")`` flushes to disk.
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- emit

    def emit(self, event: Event) -> None:
        """Serialise ``event`` and append it to the log."""
        record = event.to_json_dict()
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            if self._sync:
                try:
                    os.fsync(self._fh.fileno())
                except OSError as exc:  # pragma: no cover — best-effort durability
                    logger.debug("fsync failed on %s: %s", self.path, exc)

    # ------------------------------------------------------------- context

    def close(self) -> None:
        """Close the underlying file handle. Safe to call multiple times."""
        with self._lock:
            if not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()

    def __enter__(self) -> JsonlEventBus:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class TeeEventBus:
    """Fan-out bus that forwards every event to a list of downstream buses.

    Useful for the CLI which typically wants JSONL persistence *plus* a
    plain-log stderr renderer during the same run.
    """

    def __init__(self, *buses: EventBus) -> None:
        self._buses: list[EventBus] = list(buses)

    def emit(self, event: Event) -> None:
        for bus in self._buses:
            try:
                bus.emit(event)
            except Exception as exc:
                logger.error("Downstream bus %r raised: %s", bus, exc)

    def add(self, bus: EventBus) -> None:
        """Attach an additional downstream bus."""
        self._buses.append(bus)
