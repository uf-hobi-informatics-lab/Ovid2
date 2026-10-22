"""Event-log tail service.

Wraps :mod:`ovid2.display.reader` to expose cursor-based pages of the
per-run JSONL event log. The cursor is a byte offset so subsequent
polls only read what has been appended since the previous call.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path

from ovid2.web.models import RunEventPage
from ovid2.web.settings import RUN_EVENTS_PAGE_SIZE

logger = logging.getLogger(__name__)

_FINISHED_KIND: str = "instance.finished"


def _work_dir() -> Path:
    """Return the current ``WORK_DIR`` via a late import.

    ``ovid2.config.settings.WORK_DIR`` is computed at module import
    time. Looking it up per-call keeps tests that patch
    ``OVID2_WORK_DIR`` after the module was first imported working.
    """
    from ovid2.config.settings import WORK_DIR

    return WORK_DIR


def event_log_path(instance_id: str) -> Path:
    """Return the expected JSONL event-log path for a given instance."""
    return _work_dir() / f"events-{instance_id}.jsonl"


def latest_instance_id() -> str | None:
    """Return the instance id of the most recently modified event log."""
    work = _work_dir()
    if not work.is_dir():
        return None
    candidates = glob.glob(str(work / "events-*.jsonl"))
    if not candidates:
        return None
    most_recent = max(candidates, key=os.path.getmtime)
    stem = Path(most_recent).stem  # events-<instance_id>
    return stem[len("events-") :] if stem.startswith("events-") else None


def read_page(
    instance_id: str,
    *,
    after: int = 0,
    limit: int = RUN_EVENTS_PAGE_SIZE,
) -> RunEventPage:
    """Return events appended to the log after byte offset ``after``.

    Args:
        instance_id: Instance whose ``events-<id>.jsonl`` should be tailed.
        after: Byte offset returned by the previous call (``next_cursor``);
            ``0`` for the first page.
        limit: Soft cap on the number of events returned.

    Returns:
        A :class:`RunEventPage`. ``next_cursor`` is the byte offset the
        next call should pass; ``finished`` is ``True`` once an
        ``instance.finished`` event has been observed. When the log file
        does not exist yet, an empty page is returned with
        ``next_cursor=after``.
    """
    path = event_log_path(instance_id)
    if not path.is_file():
        return RunEventPage(
            instance_id=instance_id, next_cursor=int(after), finished=False, events=[]
        )

    events: list[dict[str, object]] = []
    finished = False
    cursor = int(after)
    try:
        with path.open(encoding="utf-8") as fh:
            fh.seek(cursor)
            while len(events) < int(limit):
                line = fh.readline()
                if not line:
                    break
                # ``readline`` may return a partial line if a writer is mid-flush.
                if not line.endswith("\n"):
                    # Rewind so the next poll retries the partial line.
                    fh.seek(cursor)
                    break
                cursor = fh.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed event in %s", path)
                    continue
                if not isinstance(record, dict):
                    continue
                events.append(record)
                if record.get("kind") == _FINISHED_KIND:
                    finished = True
                    break
    except OSError as exc:
        logger.warning("Failed to read event log %s: %s", path, exc)

    return RunEventPage(
        instance_id=instance_id, next_cursor=cursor, finished=finished, events=events
    )
