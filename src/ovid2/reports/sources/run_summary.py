"""Report-source adapter: run summary from a run's event-log aggregate.

Input is the plain-dict form of the web tier's ``RunTimeline``
(``timeline_svc.build_timeline(...).model_dump()``) — this module does
not import the web tier; the caller passes the dict. Works for every run
kind (ETL, analysis, health, copy/move).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ovid2.reports.sources import column, dataset, make_source

__all__ = ["build"]

_STAGE_ORDER = ("read", "format", "deduplicate", "transform", "map", "fix", "write", "other")


def _seconds(start: Any, end: Any) -> float | None:
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return max((b - a).total_seconds(), 0.0)


def build(
    timeline: Mapping[str, Any],
    *,
    pipeline: str | None = None,
    batch_id: str | None = None,
    run_kind: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Return a report source built from a run timeline dict."""
    instance_id = str(timeline.get("instance_id") or "")
    tables = list(timeline.get("tables") or [])
    fixes = list(timeline.get("fixes") or [])
    stages = timeline.get("stage_counts") or {}
    duration = _seconds(timeline.get("started_at"), timeline.get("ended_at"))

    table_rows = []
    for t in tables:
        ms = t.get("elapsed_ms")
        table_rows.append(
            {
                "table": t.get("table"),
                "status": t.get("status"),
                "elapsed_s": round(float(ms) / 1000.0, 3) if ms is not None else None,
                "started_at": t.get("started_at"),
                "ended_at": t.get("ended_at"),
                "error": t.get("error") or "",
            }
        )
    stage_rows = []
    for name in _STAGE_ORDER:
        s = stages.get(name)
        if not s:
            continue
        for outcome in ("started", "completed", "failed"):
            stage_rows.append(
                {"stage": name, "outcome": outcome, "events": int(s.get(outcome) or 0)}
            )
    fix_rows = [
        {
            "table": f.get("table"),
            "fix": f.get("fix"),
            "status": f.get("status"),
            "events": 1,
            "error": f.get("error") or "",
        }
        for f in fixes
    ]
    fix_counts: dict[str, dict[str, int]] = {}
    for f in fix_rows:
        bucket = fix_counts.setdefault(str(f["fix"]), {"applied": 0, "skipped": 0, "failed": 0})
        if f["status"] in bucket:
            bucket[str(f["status"])] += 1
    completed = sum(1 for t in tables if t.get("status") == "completed")
    failed = sum(1 for t in tables if t.get("status") == "failed")
    datasets = {
        "overview": dataset(
            "Run overview",
            [
                column("tables", "quantitative", "Tables"),
                column("tables_completed", "quantitative", "Completed"),
                column("tables_failed", "quantitative", "Failed"),
                column("fixes_applied", "quantitative", "Fixes applied"),
                column("warnings", "quantitative", "Warnings"),
                column("errors", "quantitative", "Errors"),
                column("duration_s", "quantitative", "Duration (s)"),
                column("success", "boolean", "Succeeded"),
            ],
            [
                {
                    "tables": len(tables),
                    "tables_completed": completed,
                    "tables_failed": failed,
                    "fixes_applied": sum(1 for f in fix_rows if f["status"] == "applied"),
                    "warnings": int(timeline.get("warning_count") or 0),
                    "errors": int(timeline.get("error_count") or 0),
                    "duration_s": duration,
                    "success": timeline.get("success"),
                }
            ],
        ),
        "tables": dataset(
            "Tables",
            [
                column("table", "nominal", "Table"),
                column("status", "nominal", "Status"),
                column("elapsed_s", "quantitative", "Elapsed (s)"),
                column("started_at", "temporal", "Started"),
                column("ended_at", "temporal", "Ended"),
                column("error", "text", "Error"),
            ],
            table_rows,
        ),
        "stages": dataset(
            "Stage events",
            [
                column("stage", "ordinal", "Stage"),
                column("outcome", "nominal", "Outcome"),
                column("events", "quantitative", "Events"),
            ],
            stage_rows,
        ),
        "fixes": dataset(
            "Fix outcomes",
            [
                column("table", "nominal", "Table"),
                column("fix", "nominal", "Fix"),
                column("status", "nominal", "Status"),
                column("events", "quantitative", "Events"),
                column("error", "text", "Error"),
            ],
            fix_rows,
        ),
        "fix_counts": dataset(
            "Fix outcomes per fix",
            [
                column("fix", "nominal", "Fix"),
                column("applied", "quantitative", "Applied"),
                column("skipped", "quantitative", "Skipped"),
                column("failed", "quantitative", "Failed"),
            ],
            [{"fix": k, **v} for k, v in sorted(fix_counts.items())],
        ),
    }
    state = {True: "succeeded", False: "failed", None: "has not finished"}[timeline.get("success")]
    overview = (
        f"Run `{instance_id}`{f' ({run_kind})' if run_kind else ''} {state}: "
        f"{completed}/{len(tables)} table(s) completed, {failed} failed"
        + (f", in {duration:,.0f} s." if duration is not None else ".")
    )
    return make_source(
        kind="run_summary",
        key=key or instance_id or "unknown",
        title=f"Run summary — {pipeline or instance_id}",
        datasets=datasets,
        texts={"overview": overview},
        instance_id=instance_id or None,
        pipeline=pipeline,
        batch_id=batch_id,
        generated_at=timeline.get("ended_at") or None,
        provenance={"run_kind": run_kind} if run_kind else {},
        templates=["run_summary.performance"],
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
