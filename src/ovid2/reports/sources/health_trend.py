"""Report-source adapter: data-health trend across several runs.

Input: a list of already-built ``health`` report sources (oldest first)
for the same pipeline. Produces refresh-over-refresh datasets — the only
cross-run combination supported in v1 (free-form joins between sources
are out of scope, see the ADR).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ovid2.reports.sources import SourceError, column, dataset, make_source

__all__ = ["build"]


def build(sources: list[Mapping[str, Any]], *, key: str | None = None) -> dict[str, Any]:
    """Return a trend source from ``health`` sources (2..50, same pipeline)."""
    if not 2 <= len(sources) <= 50:
        raise SourceError("a health trend needs between 2 and 50 health sources")
    if any(s.get("kind") != "health" for s in sources):
        raise SourceError("health_trend combines health sources only")
    pipelines = {s.get("pipeline") for s in sources}
    if len(pipelines) != 1:
        raise SourceError("all runs in a trend must belong to the same pipeline")
    ordered = sorted(sources, key=lambda s: str(s.get("generated_at") or ""))
    runs, statuses, checks = [], [], []
    for s in ordered:
        ds = s["datasets"]
        label = str(s.get("batch_id") or s.get("instance_id") or s.get("generated_at"))
        when = s.get("generated_at")
        summary = (ds.get("summary", {}).get("rows") or [{}])[0]
        runs.append(
            {"run": label, "generated_at": when, "instance_id": s.get("instance_id"), **summary}
        )
        for r in ds.get("status", {}).get("rows") or []:
            statuses.append({"run": label, "generated_at": when, **r})
        for r in ds.get("checks", {}).get("rows") or []:
            checks.append(
                {
                    "run": label,
                    "generated_at": when,
                    "check_name": r.get("check_name"),
                    "part": r.get("part"),
                    "status": r.get("status"),
                    "exception_count": r.get("exception_count"),
                }
            )
    pipeline = next(iter(pipelines))
    datasets = {
        "runs": dataset(
            "Headline counts per run",
            [
                column("run", "ordinal", "Run"),
                column("generated_at", "temporal", "Generated"),
                column("instance_id", "nominal", "Instance"),
                column("checks_total", "quantitative", "Checks run"),
                column("checks_passed", "quantitative", "Passed"),
                column("checks_exception", "quantitative", "Exceptions"),
                column("checks_error", "quantitative", "Errors"),
                column("checks_not_applicable", "quantitative", "Not applicable"),
                column("pass_rate", "quantitative", "Pass rate (%)"),
            ],
            runs,
        ),
        "status": dataset(
            "Checks by status per run",
            [
                column("run", "ordinal", "Run"),
                column("generated_at", "temporal", "Generated"),
                column("status", "nominal", "Status"),
                column("checks", "quantitative", "Checks"),
            ],
            statuses,
        ),
        "checks": dataset(
            "Check status per run",
            [
                column("run", "ordinal", "Run"),
                column("generated_at", "temporal", "Generated"),
                column("check_name", "nominal", "Check"),
                column("part", "ordinal", "Part"),
                column("status", "nominal", "Status"),
                column("exception_count", "quantitative", "Exceptions"),
            ],
            checks,
        ),
    }
    first, last = runs[0], runs[-1]
    delta = float(last.get("pass_rate") or 0) - float(first.get("pass_rate") or 0)
    overview = (
        f"Data-health trend for `{pipeline}` across **{len(runs)}** runs: pass rate "
        f"{first.get('pass_rate')}% → {last.get('pass_rate')}% ({delta:+.1f} points)."
    )
    return make_source(
        kind="health_trend",
        key=key or f"{pipeline}-{len(runs)}-{ordered[-1].get('instance_id')}",
        title=f"Data-health trend — {pipeline}",
        datasets=datasets,
        texts={"overview": overview},
        pipeline=str(pipeline) if pipeline else None,
        cdm=ordered[-1].get("cdm"),
        provenance={"sources": [s.get("source_id") for s in ordered]},
        templates=["health_trend.refresh_trend"],
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
