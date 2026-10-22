"""Plain-log renderer.

Formats events as human-readable log lines suitable for stderr or
``journalctl``. Every line is one event; no color codes, no live
overwrite.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import IO, Any

from ovid2.core.events import Event


def _format_record(record: Mapping[str, Any]) -> str:
    """Format one event record into a log line."""
    ts = record.get("ts", "")
    kind = record.get("kind", "?")
    pipeline = record.get("pipeline", "")
    batch = record.get("batch_id", "")
    table = record.get("table", "")
    check_id = record.get("check_id", "")
    part = record.get("part", "")

    prefix_parts: list[str] = [str(ts), f"[{kind}]"]
    if pipeline:
        prefix_parts.append(pipeline)
    if batch:
        prefix_parts.append(batch)
    if table:
        prefix_parts.append(f"table={table}")
    if check_id:
        prefix_parts.append(f"check={check_id}")
    if part:
        prefix_parts.append(f"part={part}")

    suffix: str
    if kind == "fix.applied":
        current = record.get("current_fix", "?")
        total = record.get("total_fixes", "?")
        suffix = f"fix {record.get('fix', '?')} ({current}/{total})"
    elif kind == "fix.failed":
        suffix = f"fix {record.get('fix', '?')} FAILED: {record.get('error', '')}"
    elif kind == "table.failed":
        suffix = f"FAILED: {record.get('error', '')}"
    elif kind == "check.completed":
        status = record.get("check_status", "")
        exceptions = record.get("exception_count", 0)
        suffix = f"status={status} exceptions={exceptions}"
    elif kind == "check.failed":
        suffix = f"FAILED: {record.get('error', '')}"
    elif kind in {"log", "warning", "error"}:
        level = record.get("level", kind)
        suffix = f"{level}: {record.get('message', '')}"
    elif kind == "stage.progress":
        stage = record.get("stage", "")
        elapsed = record.get("elapsed_ms", "")
        suffix = f"stage={stage} elapsed_ms={elapsed}"
    else:
        suffix = ""

    return " ".join(part for part in [*prefix_parts, suffix] if part)


def render_stream(records: Iterable[object], stream: IO[str] | None = None) -> None:
    """Write formatted lines for every record in ``records`` to ``stream``.

    Args:
        records: Iterable of event dicts (from
            :func:`ovid2.display.reader.read_events` or
            :func:`follow_events`).
        stream: Output stream. Defaults to ``sys.stderr``.
    """
    out = stream if stream is not None else sys.stderr
    for record in records:
        if not isinstance(record, Mapping):
            continue
        print(_format_record(record), file=out, flush=True)


class PlainLogBus:
    """In-process bus that writes plain log lines directly (no JSONL step).

    Useful as one leg of :class:`~ovid2.display.bus.TeeEventBus` when the
    user wants live stderr output alongside the persistent JSONL log.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stderr

    def emit(self, event: Event) -> None:
        record = event.to_json_dict()
        print(_format_record(record), file=self._stream, flush=True)
