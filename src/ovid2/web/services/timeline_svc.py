"""Run-detail aggregation service.

Turns the raw JSONL event stream for a run into a structured summary
the UI can render: per-stage progress, per-table status, applied fixes,
and top-level counters. Reads through :mod:`events_svc.read_page` so
the underlying file cursor semantics are unchanged.
"""

from __future__ import annotations

from typing import Any

from ovid2.web.models import (
    RunCheckSummary,
    RunFixSummary,
    RunStageSummary,
    RunTableSummary,
    RunTimeline,
)
from ovid2.web.services import events_svc

_STAGE_ORDER: tuple[str, ...] = ("read", "format", "deduplicate", "map", "fix", "write")


def build_timeline(instance_id: str) -> RunTimeline:
    """Aggregate every event currently in the log into a timeline summary.

    The whole log is walked once — a byte-offset cursor is not enough
    here because table-completed events reference table-started events
    that may have arrived earlier. Cost is bounded by the size of the
    per-run JSONL (tens of MB at the extreme).
    """
    tables: dict[str, RunTableSummary] = {}
    fixes: list[RunFixSummary] = []
    checks: dict[str, RunCheckSummary] = {}
    stage_counts: dict[str, dict[str, int]] = {
        stage: {"started": 0, "completed": 0, "failed": 0} for stage in _STAGE_ORDER
    }
    stage_counts["other"] = {"started": 0, "completed": 0, "failed": 0}
    started_at: str | None = None
    ended_at: str | None = None
    success: bool | None = None
    warning_count = 0
    error_count = 0

    cursor = 0
    events_seen = 0
    while True:
        page = events_svc.read_page(instance_id, after=cursor, limit=2000)
        if not page.events:
            break
        events_seen += len(page.events)
        for ev in page.events:
            _apply_event(ev, tables, fixes, stage_counts, checks)
            kind = ev.get("kind")
            ts = ev.get("ts")
            level = ev.get("level")
            if kind == "instance.started" and isinstance(ts, str):
                started_at = ts
            elif kind == "instance.finished" and isinstance(ts, str):
                ended_at = ts
                extra = ev.get("extra")
                if isinstance(extra, dict):
                    success = bool(extra.get("success"))
            elif kind == "warning" or level == "warning":
                warning_count += 1
            elif kind == "error" or level == "error":
                error_count += 1
        cursor = page.next_cursor
        if page.finished or len(page.events) < 2000:
            # ``events_svc`` may stop early when it hits ``instance.finished``.
            # Also stop when the read returned fewer than the limit so we
            # don't spin on a live-tailed file.
            if page.finished:
                break
            if len(page.events) < 2000:
                break

    return RunTimeline(
        instance_id=instance_id,
        started_at=started_at,
        ended_at=ended_at,
        success=success,
        events_seen=events_seen,
        warning_count=warning_count,
        error_count=error_count,
        stage_counts={
            stage: RunStageSummary(
                stage=stage,
                started=counts["started"],
                completed=counts["completed"],
                failed=counts["failed"],
            )
            for stage, counts in stage_counts.items()
            if any(counts.values())
        },
        tables=[tables[name] for name in sorted(tables.keys())],
        fixes=fixes,
        checks=list(checks.values()),
    )


def _apply_event(
    ev: dict[str, Any],
    tables: dict[str, RunTableSummary],
    fixes: list[RunFixSummary],
    stage_counts: dict[str, dict[str, int]],
    checks: dict[str, RunCheckSummary] | None = None,
) -> None:
    """Fold one event into the aggregation state."""
    kind = ev.get("kind")
    stage_raw = ev.get("stage")
    stage = str(stage_raw) if isinstance(stage_raw, str) and stage_raw in _STAGE_ORDER else "other"

    if checks is not None and isinstance(kind, str) and kind.startswith("check."):
        _apply_check_event(ev, kind, checks)
        return

    table_name = ev.get("table")
    if isinstance(table_name, str) and table_name:
        table = tables.setdefault(
            table_name,
            RunTableSummary(table=table_name, status="pending"),
        )
    else:
        table = None

    if kind == "table.started" and table is not None:
        table.status = "running"
        started_ts = ev.get("ts")
        if isinstance(started_ts, str):
            table.started_at = started_ts
        stage_counts[stage]["started"] += 1
    elif kind == "table.completed" and table is not None:
        table.status = "completed"
        ended_ts = ev.get("ts")
        if isinstance(ended_ts, str):
            table.ended_at = ended_ts
        elapsed = ev.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            table.elapsed_ms = int(elapsed)
        stage_counts[stage]["completed"] += 1
    elif kind == "table.failed" and table is not None:
        table.status = "failed"
        error = ev.get("error")
        if isinstance(error, str):
            table.error = error
        stage_counts[stage]["failed"] += 1
    elif kind == "fix.applied":
        fix_name = ev.get("fix")
        if isinstance(fix_name, str):
            fixes.append(
                RunFixSummary(
                    table=str(table_name or ""),
                    fix=fix_name,
                    status="applied",
                )
            )
    elif kind == "fix.failed":
        fix_name = ev.get("fix")
        if isinstance(fix_name, str):
            fixes.append(
                RunFixSummary(
                    table=str(table_name or ""),
                    fix=fix_name,
                    status="failed",
                    error=str(ev.get("error") or ""),
                )
            )
    elif kind == "fix.skipped":
        fix_name = ev.get("fix")
        if isinstance(fix_name, str):
            fixes.append(
                RunFixSummary(
                    table=str(table_name or ""),
                    fix=fix_name,
                    status="skipped",
                )
            )


def _apply_check_event(
    ev: dict[str, Any],
    kind: str,
    checks: dict[str, RunCheckSummary],
) -> None:
    """Fold a ``check.*`` event into the per-check aggregation state."""
    check_id = ev.get("check_id")
    if not isinstance(check_id, str) or not check_id:
        return
    summary = checks.setdefault(
        check_id,
        RunCheckSummary(
            check_id=check_id,
            check_name=str(ev.get("check_name") or ""),
            part=str(ev.get("part") or ""),
            status="running",
        ),
    )
    name = ev.get("check_name")
    if isinstance(name, str) and name:
        summary.check_name = name
    part = ev.get("part")
    if isinstance(part, str) and part:
        summary.part = part
    if kind == "check.completed":
        status = ev.get("check_status")
        summary.status = str(status) if isinstance(status, str) else "passed"
        exc = ev.get("exception_count")
        if isinstance(exc, int):
            summary.exception_count = exc
        mc = ev.get("metric_count")
        if isinstance(mc, int):
            summary.metric_count = mc
    elif kind == "check.failed":
        summary.status = "error"
        summary.error = str(ev.get("error") or "")
