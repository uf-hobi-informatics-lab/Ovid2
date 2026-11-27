"""Per-item run timings (tables / checks) — saved, then served to the run page.

The run console shows a clock per table (ETL / copy / move / analysis) or per
check (health / dq_checks). Those clocks used to be rebuilt in the browser
by replaying the whole event log, timing each item from the moment its
``*.started`` line was *read*. Two problems came from that: finished items
ticked for a few seconds (their end events arrived several polls later),
and running items restarted at ``00:00`` on every page open.

This service folds the run's event log into one row per item —
``status``, ``started_at``, ``ended_at``, ``elapsed_ms``, ``error`` — and
saves it in ``web_run_items`` (the event log lives on tmpfs and can
vanish). The run page embeds the rows at render time, so every clock
starts from its real, server-recorded times the moment the page opens.

Sync points:

* the web worker, when it finalises a job (:func:`sync_from_log`);
* the run page render, for a run that is still live or has no saved rows
  yet (:func:`times_for_page`).

The fold only parses ``table.*`` / ``check.*`` / per-table
``stage.progress`` lines (the log is mostly ``engine.metrics`` samples), so
re-reading a long log stays cheap.

Multi-phase runs (the ``legacy_ovid`` runner maps every table, *then* fixes,
*then* publishes) bracket each phase with per-table ``stage.progress``
events whose ``extra.state`` is ``waiting`` / ``active``
(:func:`ovid2.core.events.table_stage`). A waiting table's clock is
paused: ``elapsed_ms`` holds the active time accumulated so far and
``active_since`` the start of the current active segment, so a table's
time is the time it was actually being worked on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ovid2.web import db as web_db
from ovid2.web.services import events_svc
from ovid2.workspaces import active_workspace

logger = logging.getLogger(__name__)

__all__ = [
    "ITEM_CHECK",
    "ITEM_STAGE",
    "ITEM_TABLE",
    "RunItemTime",
    "close_open_items",
    "ensure_finished_event",
    "finalize_run",
    "fold_events",
    "get_items",
    "sync_from_log",
    "times_for_page",
]

#: Item kinds (one per run-console flavour).
ITEM_TABLE = "table"
ITEM_CHECK = "check"
#: Per-table pipeline stage (``<table>/<stage>``), folded from the
#: ``stage.progress`` *enter* events of ``core/pipeline.py``. Read by the
#: Atlas (live transformation chain, heat overlay); the run console ignores
#: them (it filters on its own item kind).
ITEM_STAGE = "stage"

_TERMINAL = frozenset({"completed", "failed", "passed", "exception", "error", "cancelled"})
#: Item statuses that are still open (their clock would keep ticking).
_OPEN = frozenset({"running", "waiting"})
#: Job statuses after which nothing in the run is executing.
_JOB_TERMINAL = frozenset({"done", "failed", "cancelled"})


@dataclass
class RunItemTime:
    """Saved timing of one run item.

    Attributes:
        item_kind: ``table`` or ``check``.
        item: Table name / check id.
        display_name: Human name (checks); ``None`` for tables.
        status: ``running`` | ``waiting`` | ``completed`` | ``failed``
            (tables) or ``running`` | ``passed`` | ``exception`` | ``error``
            (checks). ``waiting`` = started, paused between phases.
        started_at: ISO-8601 UTC start (the ``*.started`` event ``ts``).
        ended_at: ISO-8601 UTC end (the terminal event ``ts``).
        elapsed_ms: Duration — the executor-measured ``elapsed_ms`` when
            present, else the summed active segments (``ended_at -
            started_at`` for a single-phase item). For a running / waiting
            item: the active time accumulated *before* ``active_since``.
        error: Failure message, when any.
        seq: First-appearance order in the log.
        active_since: Start of the current active segment (running items);
            ``None`` while waiting or once terminal.
    """

    item_kind: str
    item: str
    display_name: str | None = None
    status: str = "running"
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    seq: int = 0
    active_since: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the browser-facing dict (the run page's JSON island)."""
        return {
            "kind": self.item_kind,
            "item": self.item,
            "display_name": self.display_name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "active_since": self.active_since,
        }


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _segment_ms(since: str | None, until: Any) -> int:
    """Milliseconds from ``since`` to ``until`` (0 when unknown / negative)."""
    start, end = _parse_ts(since), _parse_ts(until)
    if start is None or end is None or end < start:
        return 0
    return int((end - start).total_seconds() * 1000)


def _pause(rec: RunItemTime, ts: Any) -> None:
    """Close the item's current active segment at ``ts``."""
    if rec.active_since is not None:
        rec.elapsed_ms = (rec.elapsed_ms or 0) + _segment_ms(rec.active_since, ts)
        rec.active_since = None


def _finish(rec: RunItemTime, ev: dict[str, Any], status: str) -> None:
    ts = ev.get("ts")
    was_paused = rec.status == "waiting"
    rec.status = status
    if isinstance(ts, str):
        rec.ended_at = ts
    elapsed = ev.get("elapsed_ms")
    if isinstance(elapsed, (int, float)) and elapsed >= 0:
        rec.elapsed_ms = int(elapsed)
        rec.active_since = None
        return
    if rec.active_since is None and rec.elapsed_ms is None and not was_paused:
        # Single-phase item: no stage events, time the whole span.
        rec.active_since = rec.started_at
    _pause(rec, ts)


def fold_events(
    events: list[dict[str, Any]], *, job_status: str | None = None
) -> list[RunItemTime]:
    """Fold ``table.*`` / ``check.*`` events into one record per item.

    Status is monotonic: once an item is terminal, a later (replayed /
    duplicate) ``*.started`` does not reopen it. An ``instance.finished``
    line (the process is exiting) closes every item still open.

    Args:
        events: Decoded event-log records, in log order.
        job_status: The job's terminal status when known; decides whether
            items closed by ``instance.finished`` read ``cancelled`` or
            ``failed``.

    Returns:
        Records in first-appearance order.
    """
    items: dict[tuple[str, str], RunItemTime] = {}

    def _rec(kind: str, key: str) -> RunItemTime:
        rec = items.get((kind, key))
        if rec is None:
            rec = RunItemTime(item_kind=kind, item=key, seq=len(items))
            items[(kind, key)] = rec
        return rec

    run_end: str | None = None
    end_status = job_status or "failed"
    for ev in events:
        kind = ev.get("kind")
        if not isinstance(kind, str):
            continue
        if kind == "instance.finished":
            ts = ev.get("ts")
            run_end = ts if isinstance(ts, str) else run_end
            extra = ev.get("extra")
            stamped = extra.get("job_status") if isinstance(extra, dict) else None
            if job_status is None and isinstance(stamped, str) and stamped:
                end_status = stamped
            continue
        if kind.startswith("table."):
            key = ev.get("table")
            if not isinstance(key, str) or not key:
                continue
            rec = _rec(ITEM_TABLE, key)
            if kind == "table.started":
                if rec.status in _TERMINAL:
                    continue
                rec.status = "running"
                ts = ev.get("ts")
                rec.started_at = ts if isinstance(ts, str) else rec.started_at
                rec.active_since = rec.started_at
            elif kind == "table.completed":
                _finish(rec, ev, "completed")
                _close_stage(items, key, ev, "completed")
            elif kind == "table.failed":
                rec.error = str(ev.get("error") or "") or None
                _finish(rec, ev, "failed")
                _close_stage(items, key, ev, "failed")
        elif kind == "stage.progress":
            _apply_stage(items, ev)
            _apply_stage_step(items, ev, _rec)
        elif kind.startswith("check."):
            key = ev.get("check_id")
            if not isinstance(key, str) or not key:
                continue
            rec = _rec(ITEM_CHECK, key)
            name = ev.get("check_name")
            if isinstance(name, str) and name:
                rec.display_name = name
            if kind == "check.started":
                if rec.status in _TERMINAL:
                    continue
                rec.status = "running"
                ts = ev.get("ts")
                rec.started_at = ts if isinstance(ts, str) else rec.started_at
            elif kind == "check.completed":
                status = ev.get("check_status")
                _finish(rec, ev, str(status) if isinstance(status, str) and status else "passed")
            elif kind == "check.failed":
                rec.error = str(ev.get("error") or "") or None
                _finish(rec, ev, "error")
    records = sorted(items.values(), key=lambda r: r.seq)
    if run_end is not None:
        # The process said it is exiting: nothing can still be running.
        close_open_items(records, ended_at=run_end, job_status=end_status)
    return records


def close_open_items(
    records: list[RunItemTime], *, ended_at: str | None, job_status: str
) -> list[RunItemTime]:
    """Close every still-open (running / waiting) item at the run's end.

    A run that ends without an item's terminal event (crash, SIGKILL,
    cancel, a table stuck between phases) would otherwise leave that
    item's clock ticking forever on the run page. Each open item becomes
    ``cancelled`` (job cancelled) or ``failed`` (anything else), ends at
    ``ended_at`` and keeps only its real active time.

    Args:
        records: Folded / saved records (modified in place).
        ended_at: ISO-8601 UTC end of the run (``None`` = now).
        job_status: The job's terminal status (``done`` / ``failed`` /
            ``cancelled``).

    Returns:
        The same list, for chaining.
    """
    end = ended_at or datetime.now(UTC).isoformat()
    closed_status = "cancelled" if job_status == "cancelled" else "failed"
    for rec in records:
        if rec.status not in _OPEN:
            continue
        if rec.active_since is not None:
            _pause(rec, end)
        elif rec.elapsed_ms is None and rec.status == "running":
            rec.elapsed_ms = _segment_ms(rec.started_at, end)
        rec.status = closed_status
        rec.ended_at = end
        rec.active_since = None
        if not rec.error:
            rec.error = (
                "run cancelled before this item finished"
                if closed_status == "cancelled"
                else "run ended before this item finished"
            )
    return records


def _apply_stage(items: dict[tuple[str, str], RunItemTime], ev: dict[str, Any]) -> None:
    """Pause / resume a started table on a per-table ``stage.progress``."""
    key = ev.get("table")
    extra = ev.get("extra")
    state = extra.get("state") if isinstance(extra, dict) else None
    if not isinstance(key, str) or state not in ("active", "waiting"):
        return
    rec = items.get((ITEM_TABLE, key))
    if rec is None or rec.status in _TERMINAL:
        return  # never started (or already over): nothing to time
    ts = ev.get("ts")
    if state == "waiting":
        if rec.status == "running":
            _pause(rec, ts)
            rec.status = "waiting"
    elif rec.status == "waiting":
        rec.status = "running"
        rec.active_since = ts if isinstance(ts, str) else None


def _open_stage(items: dict[tuple[str, str], RunItemTime], table: str) -> RunItemTime | None:
    """Return the stage item of ``table`` that is still running, if any."""
    for (kind, key), rec in items.items():
        if kind == ITEM_STAGE and key.startswith(table + "/") and rec.status == "running":
            return rec
    return None


def _close_stage(
    items: dict[tuple[str, str], RunItemTime], table: str, ev: dict[str, Any], status: str
) -> None:
    """End ``table``'s running stage at ``ev`` (its next stage / its end)."""
    rec = _open_stage(items, table)
    if rec is None:
        return
    rec.status = status
    ts = ev.get("ts")
    rec.ended_at = ts if isinstance(ts, str) else rec.ended_at
    rec.elapsed_ms = _segment_ms(rec.started_at, ts)
    rec.active_since = None


def _apply_stage_step(
    items: dict[tuple[str, str], RunItemTime],
    ev: dict[str, Any],
    rec_for: Any,
) -> None:
    """Fold a per-table stage *enter* event into ``<table>/<stage>`` items."""
    extra = ev.get("extra")
    if not isinstance(extra, dict) or extra.get("step") != "enter":
        return
    table, stage = ev.get("table"), ev.get("stage")
    if not isinstance(table, str) or not isinstance(stage, str) or not table:
        return
    parent = items.get((ITEM_TABLE, table))
    if parent is None or parent.status in _TERMINAL:
        return
    _close_stage(items, table, ev, "completed")
    rec = rec_for(ITEM_STAGE, f"{table}/{stage}")
    if rec.status in _TERMINAL:
        return
    ts = ev.get("ts")
    rec.status = "running"
    rec.started_at = ts if isinstance(ts, str) else None
    rec.active_since = rec.started_at
    rec.display_name = "lazy" if extra.get("lazy") else "exec"


def _read_item_events(instance_id: str) -> list[dict[str, Any]] | None:
    """Return the log's ``table.*`` / ``check.*`` events, or ``None`` if no log."""
    path = events_svc.event_log_path(instance_id)
    if not path.is_file():
        return None
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                # Cheap pre-filter: skip the engine.metrics bulk unparsed.
                if (
                    '"table.' not in line
                    and '"check.' not in line
                    and '"stage.progress"' not in line
                    and '"instance.finished"' not in line
                ):
                    continue
                if not line.endswith("\n"):
                    break  # partial trailing line of a live log
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError as exc:
        logger.warning("Could not read event log for %s: %s", instance_id, exc)
        return None
    return out


def _save(instance_id: str, records: list[RunItemTime]) -> None:
    """Upsert ``records`` for ``instance_id`` in the active workspace."""
    if not records:
        return
    now = datetime.now(UTC).isoformat()
    ws = active_workspace()
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.executemany(
            "INSERT INTO web_run_items (workspace_id, instance_id, item_kind, item, "
            "display_name, status, started_at, ended_at, elapsed_ms, error, seq, "
            "active_since, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (workspace_id, instance_id, item_kind, item) DO UPDATE SET "
            "display_name = excluded.display_name, status = excluded.status, "
            "started_at = excluded.started_at, ended_at = excluded.ended_at, "
            "elapsed_ms = excluded.elapsed_ms, error = excluded.error, "
            "seq = excluded.seq, active_since = excluded.active_since, "
            "updated_at = excluded.updated_at",
            [
                (
                    ws,
                    instance_id,
                    r.item_kind,
                    r.item,
                    r.display_name,
                    r.status,
                    r.started_at,
                    r.ended_at,
                    r.elapsed_ms,
                    r.error,
                    r.seq,
                    r.active_since,
                    now,
                )
                for r in records
            ],
        )


def sync_from_log(
    instance_id: str,
    *,
    job_status: str | None = None,
    ended_at: str | None = None,
) -> list[RunItemTime] | None:
    """Fold the run's event log and save the result.

    Args:
        instance_id: The run's instance id.
        job_status: The job's status when known. A terminal status closes
            every item still open (see :func:`close_open_items`).
        ended_at: The job's ``ended_at`` (ISO-8601 UTC), used as the end
            of those closed items.

    Returns:
        The folded records, or ``None`` when the log does not exist (the
        saved rows, if any, are left untouched).
    """
    events = _read_item_events(instance_id)
    if events is None:
        return None
    records = fold_events(events, job_status=job_status if job_status in _JOB_TERMINAL else None)
    if job_status in _JOB_TERMINAL:
        close_open_items(records, ended_at=ended_at, job_status=str(job_status))
    try:
        _save(instance_id, records)
    except Exception as exc:  # timings are advisory; never break the caller
        logger.warning("Saving run item times for %s failed: %s", instance_id, exc)
    return records


def get_items(instance_id: str) -> list[RunItemTime]:
    """Return the saved records for ``instance_id`` (active workspace)."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT item_kind, item, display_name, status, started_at, ended_at, "
            "elapsed_ms, error, seq, active_since FROM web_run_items "
            "WHERE workspace_id = ? AND instance_id = ? ORDER BY seq",
            (active_workspace(), instance_id),
        ).fetchall()
    return [
        RunItemTime(
            item_kind=str(r["item_kind"]),
            item=str(r["item"]),
            display_name=r["display_name"],
            status=str(r["status"]),
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            elapsed_ms=r["elapsed_ms"],
            error=r["error"],
            seq=int(r["seq"] or 0),
            active_since=r["active_since"],
        )
        for r in rows
    ]


def times_for_page(
    instance_id: str,
    *,
    live: bool,
    job_status: str | None = None,
    ended_at: str | None = None,
) -> list[dict[str, Any]]:
    """Return the item timings the run page embeds at render time.

    A live run (or one with nothing saved yet) is folded from its log and
    saved; a finished run is served from the saved rows (its log may be
    gone). For a finished run any row still open is closed at the job's
    end first, so no clock on the page can tick after the job is over.
    Never raises — the page then falls back to the event replay.

    Args:
        instance_id: The run's instance id.
        live: ``True`` while the job is queued / running.
        job_status: The job's status (closes open rows when terminal).
        ended_at: The job's ``ended_at`` (ISO-8601 UTC).

    Returns:
        ``RunItemTime.to_json()`` dicts in first-appearance order.
    """
    try:
        if not live:
            saved = get_items(instance_id)
            if saved:
                if job_status in _JOB_TERMINAL and any(r.status in _OPEN for r in saved):
                    close_open_items(saved, ended_at=ended_at, job_status=str(job_status))
                    _save(instance_id, saved)
                return [r.to_json() for r in saved]
        folded = sync_from_log(
            instance_id,
            job_status=None if live else job_status,
            ended_at=None if live else ended_at,
        )
        if folded is not None:
            return [r.to_json() for r in folded]
        return [r.to_json() for r in get_items(instance_id)] if live else []
    except Exception as exc:
        logger.warning("Run item times for %s unavailable: %s", instance_id, exc)
        return []


def ensure_finished_event(instance_id: str, *, job_status: str, ended_at: str | None) -> bool:
    """Append a closing ``instance.finished`` to a log that lacks one.

    A process that is SIGKILLed / OOM-killed / loses its container never
    writes ``instance.finished``; every open run page would then keep
    polling and its clocks keep counting. The worker calls this when it
    finalises the job so the event stream itself says the run is over.

    Args:
        instance_id: The run's instance id.
        job_status: The job's terminal status.
        ended_at: ISO-8601 UTC end (``None`` = now).

    Returns:
        ``True`` when an event was appended.
    """
    if job_status not in _JOB_TERMINAL:
        return False
    path = events_svc.event_log_path(instance_id)
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
        if '"kind":"instance.finished"' in tail.replace(" ", ""):
            return False
        record = {
            "v": 1,
            "kind": "instance.finished",
            "ts": ended_at or datetime.now(UTC).isoformat(),
            "instance_id": instance_id,
            "extra": {"success": job_status == "done", "job_status": job_status, "synthetic": True},
        }
        prefix = "" if not tail or tail.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except OSError as exc:
        logger.warning("Could not close event log for %s: %s", instance_id, exc)
        return False


def finalize_run(instance_id: str, *, job_status: str, ended_at: str | None) -> None:
    """Close a finished run's log + saved item rows (never raises).

    Called by the web worker on finalise and by crash recovery.

    Args:
        instance_id: The run's instance id.
        job_status: The job's terminal status.
        ended_at: The job's ``ended_at`` (ISO-8601 UTC).
    """
    try:
        ensure_finished_event(instance_id, job_status=job_status, ended_at=ended_at)
        folded = sync_from_log(instance_id, job_status=job_status, ended_at=ended_at)
        if folded is None:
            saved = get_items(instance_id)
            if any(r.status in _OPEN for r in saved):
                close_open_items(saved, ended_at=ended_at, job_status=job_status)
                _save(instance_id, saved)
    except Exception as exc:  # timings are advisory; never break the caller
        logger.warning("Finalising run items for %s failed: %s", instance_id, exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.web.services.run_items_svc is a library module.")
