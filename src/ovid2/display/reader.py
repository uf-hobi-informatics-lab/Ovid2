"""Event-log reader.

Renderers (``ovid2 watch`` TUI, journald bridge, email digest, tests)
consume events by tailing an instance's JSONL log file. The reader
supports two modes:

- **Batch**: :func:`read_events` returns every event currently in the
  file and stops.
- **Follow** (``tail -f`` semantics): :func:`follow_events` yields events
  as they are written, waking up either on file changes (inotify when
  available) or a small polling interval.

Concurrency-safe with :class:`ovid2.display.bus.JsonlEventBus`: writes
are one line each, so a reader that pauses mid-file resumes cleanly.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from ovid2.config.settings import WORK_DIR

logger = logging.getLogger(__name__)


def read_events(path: Path) -> Iterator[dict[str, object]]:
    """Yield every parsable event currently in ``path``.

    Malformed lines (truncated JSON, missing envelope keys) are skipped
    with a WARNING log — the reader is deliberately permissive so a
    partial write never blocks consumption.

    Args:
        path: JSONL event log file.

    Yields:
        Parsed event records (dicts with ``kind``, ``ts``, ``instance_id``, …).
    """
    if not path.is_file():
        return

    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed event at %s:%d: %s", path, line_no, exc)
                continue
            if not isinstance(record, dict):
                logger.warning("Skipping non-object event at %s:%d", path, line_no)
                continue
            yield record


def follow_events(
    path: Path,
    *,
    poll_interval: float = 0.5,
    stop_when: object | None = None,
) -> Iterator[dict[str, object]]:
    """Yield events as they are appended to ``path`` (``tail -f`` style).

    The reader opens the file, seeks to the current end, then polls for
    new lines. If the file rotates (i.e. inode changes), the reader
    re-opens transparently.

    Args:
        path: JSONL event log file. May not exist yet — the reader waits.
        poll_interval: Seconds between filesystem polls when idle.
        stop_when: Optional callable returning True to end the stream.
            Useful for tests. Also honours ``instance.finished`` events
            when this is ``None``.

    Yields:
        Parsed event records.
    """
    # Wait for the file to appear.
    while not path.is_file():
        if _should_stop(stop_when):
            return
        time.sleep(poll_interval)

    inode = path.stat().st_ino
    fh = path.open(encoding="utf-8")
    try:
        # Start of the file; consumers get the full run history.
        while True:
            line = fh.readline()
            if line:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
                    # Auto-stop when the pipeline signals it is done.
                    if stop_when is None and record.get("kind") == "instance.finished":
                        return
                    if _should_stop(stop_when):
                        return
                continue

            # No new data; check for file rotation, then sleep.
            if _should_stop(stop_when):
                return
            try:
                new_inode = path.stat().st_ino
            except FileNotFoundError:
                new_inode = None
            if new_inode is not None and new_inode != inode:
                fh.close()
                fh = path.open(encoding="utf-8")
                inode = new_inode
                continue

            time.sleep(poll_interval)
    finally:
        fh.close()


def _should_stop(stop_when: object | None) -> bool:
    """Return True if the optional stop-predicate says to stop."""
    if stop_when is None:
        return False
    if callable(stop_when):
        try:
            return bool(stop_when())
        except Exception:
            return True
    return False


def latest_event_log() -> Path | None:
    """Return the most recently modified ``events-*.jsonl`` in :data:`WORK_DIR`.

    Useful for ``ovid2 watch --follow-latest``. Returns ``None`` if no log
    is present.
    """
    if not WORK_DIR.is_dir():
        return None
    candidates = glob.glob(str(WORK_DIR / "events-*.jsonl"))
    if not candidates:
        return None
    most_recent = max(candidates, key=lambda p: os.path.getmtime(p))
    return Path(most_recent)
