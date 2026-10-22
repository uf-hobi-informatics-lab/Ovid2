"""Live-engine-metrics service.

Two responsibilities:

* :func:`latest` — return the most recent ``engine.metrics`` sample as
  a plain dict, or ``None`` if no sample has been written yet. Reads
  the JSONL event log backwards from the end (bounded scan) so it's
  cheap even on long-running jobs.
* :func:`stream` — a generator that yields SSE-formatted bytes as new
  ``engine.metrics`` samples appear. The generator terminates when the
  run's ``instance.finished`` event is observed (or when the caller
  disconnects — FastAPI cancels the generator).

Both functions consult :mod:`ovid2.web.services.events_svc` for the
event-log path so log rotation / instance-id resolution stays in one
place.

SSE format
----------

* Data samples are emitted as ``event: sample\\ndata: <json>\\n\\n``.
* Terminal frames use ``event: finished\\ndata: {}\\n\\n``.
* Keepalives are SSE comment lines (``: keepalive\\n\\n``) sent every
  ``LIVE_SSE_KEEPALIVE_S`` seconds so reverse proxies don't cut idle
  connections during long stretches with no metric emission (e.g.
  during Snowpark's 5 s polling interval).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ovid2.web.services import events_svc
from ovid2.web.settings import (
    LIVE_SSE_KEEPALIVE_S,
    LIVE_SSE_MAX_SECONDS,
)

logger = logging.getLogger(__name__)

_METRICS_KIND: str = "engine.metrics"
_FINISHED_KIND: str = "instance.finished"

#: Maximum tail size (bytes) considered by :func:`latest`. The whole
#: JSONL file may be arbitrarily long; the last sample is guaranteed to
#: live within the last few KB, so we bound the scan.
_LATEST_TAIL_BYTES: int = 65_536


def latest(instance_id: str) -> dict[str, Any] | None:
    """Return the most recent ``engine.metrics`` sample for a run.

    Reads the JSONL log from the end (bounded 64 KB window) and returns
    the last line whose ``kind`` is ``engine.metrics``. Returns
    ``None`` when no sample is present — the sampler starts a few
    hundred milliseconds after the session comes up, so early
    HTMX-poll requests during job start-up will legitimately get
    ``None`` for a moment.

    Args:
        instance_id: The run's instance id.

    Returns:
        A JSON-shaped dict matching the event bus payload, or
        ``None``.
    """
    path = events_svc.event_log_path(instance_id)
    if not path.is_file():
        return None

    try:
        size = path.stat().st_size
        start = max(0, size - _LATEST_TAIL_BYTES)
        with path.open("rb") as fh:
            fh.seek(start)
            tail = fh.read()
    except OSError as exc:
        logger.debug("metrics latest: read failed for %s: %s", path, exc)
        return None

    # Walk lines in reverse; the last line may be partial (writer
    # mid-flush) — skip anything that doesn't parse as a dict.
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("kind") == _METRICS_KIND:
            return record
    return None


def stream(
    instance_id: str,
    *,
    max_seconds: int | None = None,
    keepalive_s: int | None = None,
) -> Iterator[bytes]:
    """Yield SSE bytes for a run's live metrics stream.

    Args:
        instance_id: The run's instance id.
        max_seconds: Hard upper bound on stream lifetime. Prevents a
            forgotten browser tab from holding a worker forever.
            Defaults to :data:`LIVE_SSE_MAX_SECONDS`.
        keepalive_s: Interval between SSE comment keepalives.
            Defaults to :data:`LIVE_SSE_KEEPALIVE_S`.

    Yields:
        SSE-framed bytes suitable for a FastAPI ``StreamingResponse``.
    """
    if max_seconds is None:
        max_seconds = LIVE_SSE_MAX_SECONDS
    if keepalive_s is None:
        keepalive_s = LIVE_SSE_KEEPALIVE_S

    path = events_svc.event_log_path(instance_id)
    started = time.monotonic()
    last_ping = started

    # Yield a comment on first tick so the client sees the connection
    # is live even before the first sample arrives.
    yield b": connected\n\n"

    # Send the most recent sample immediately (if any) so the UI
    # doesn't have to wait for the next 500 ms tick before showing
    # anything.
    primed = latest(instance_id)
    if primed is not None:
        yield _frame_sample(primed)

    cursor = 0
    finished = False

    # If the log file exists, start reading from the current end so we
    # don't re-emit historical samples the client already got via
    # ``primed``. Before jumping to EOF, scan the tail for a terminal
    # ``instance.finished`` event so a stream opened *after* the run
    # already finished doesn't spin until timeout.
    if path.is_file():
        try:
            cursor = path.stat().st_size
        except OSError:
            cursor = 0
        try:
            # Bounded backwards scan — a run's tail is well under 64 KB.
            with path.open("rb") as fh:
                fh.seek(max(0, cursor - _LATEST_TAIL_BYTES))
                tail = fh.read()
            for raw in reversed(tail.splitlines()):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == _FINISHED_KIND:
                    yield b"event: finished\ndata: {}\n\n"
                    return
        except OSError:
            pass

    while not finished:
        # Time-out guard.
        now = time.monotonic()
        if now - started > max_seconds:
            yield b": timeout\n\n"
            break

        # Keepalive.
        if now - last_ping >= keepalive_s:
            yield b": keepalive\n\n"
            last_ping = now

        # Read whatever's appended since ``cursor``.
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    fh.seek(cursor)
                    chunk = fh.read()
                    cursor = fh.tell()
            except OSError as exc:
                logger.debug("metrics stream: read failed: %s", exc)
                chunk = b""
        else:
            chunk = b""

        if chunk:
            # ``chunk`` may split a line mid-write — retain any
            # incomplete tail for the next iteration.
            lines = chunk.splitlines(keepends=True)
            if lines and not lines[-1].endswith(b"\n"):
                # Rewind cursor by that partial tail so we re-read it
                # next round.
                cursor -= len(lines[-1])
                lines = lines[:-1]
            for raw in lines:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("kind")
                if kind == _METRICS_KIND:
                    yield _frame_sample(record)
                    last_ping = time.monotonic()
                elif kind == _FINISHED_KIND:
                    yield b"event: finished\ndata: {}\n\n"
                    finished = True
                    break
        else:
            # No new bytes → sleep briefly so we don't spin at 100% CPU.
            # The sleep is short enough that stream tear-down (client
            # disconnect) still surfaces within a second.
            time.sleep(0.25)


def _frame_sample(record: dict[str, Any]) -> bytes:
    """Frame a JSON sample dict as an SSE ``event: sample`` frame."""
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    return f"event: sample\ndata: {payload}\n\n".encode("utf-8")


def briefing_cooking() -> list[dict[str, Any]]:
    """Return a compact briefing of currently-cooking runs.

    Cross-service helper for the dashboard's one-line "cooking now"
    sentence. Reads the ``web_jobs`` DB for status='running' rows and
    joins each with :func:`latest`.

    Returns a list of ``{job_id, instance_id, pipeline, batch_id,
    started_at, sample}`` dicts. When no run is cooking, returns an
    empty list — the dashboard renders nothing in that case.
    """
    from ovid2.web.services import jobs_svc

    out: list[dict[str, Any]] = []
    try:
        page = jobs_svc.list_jobs(status="running", limit=25)
    except Exception:  # noqa: BLE001
        return []
    for row in page.jobs:
        instance_id = row.instance_id or ""
        if not instance_id:
            continue
        sample = latest(instance_id)
        out.append(
            {
                "job_id": row.job_id,
                "instance_id": instance_id,
                "pipeline": row.pipeline,
                "batch_id": row.batch_id,
                "started_at": row.started_at,
                "sample": sample,
            }
        )
    return out


__all__ = ["briefing_cooking", "latest", "stream"]
