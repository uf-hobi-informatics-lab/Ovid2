"""Report-source adapter: data-health / EDC report (``health-report-*.json``).

Turns the health report artifact (``health_report.v1.json``) into
datasets a researcher can chart without touching the raw JSON:

- ``summary``    — one row of headline counts (+ pass rate);
- ``status``     — check counts per status (donut / bar);
- ``parts``      — per-part counts per status (stacked bar / heatmap);
- ``checks``     — one row per check (table, status colouring);
- ``metrics``    — one row per metric (scatter / heatmap / table);
- ``by_table``   — exceptions per source table (bar / heatmap);
- ``exceptions`` — exception-flagged metrics only.

Metric ``details`` and ``value_str`` can echo source values, so they are
tagged ``sensitivity: row_values``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from ovid2.reports.sources import column, dataset, make_source

__all__ = ["build"]

_STATUSES = ("passed", "exception", "error", "skipped", "not_applicable")


def _overview(report: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    total = int(summary.get("checks_total") or 0)
    passed = int(summary.get("checks_passed") or 0)
    exc = int(summary.get("checks_exception") or 0)
    err = int(summary.get("checks_error") or 0)
    rate = (passed / total) if total else 0.0
    lines = [
        f"This report summarises **{total}** data-health checks run against "
        f"pipeline `{report.get('pipeline') or '—'}` (CDM `{report.get('cdm') or '—'}`).",
        "",
        f"- **{passed}** passed ({rate:.0%}), **{exc}** raised exceptions and **{err}** errored.",
    ]
    worst = sorted(
        (c for c in report.get("checks") or [] if c.get("status") == "exception"),
        key=lambda c: -int(c.get("exception_count") or 0),
    )[:5]
    if worst:
        lines.append(
            "- Most exceptions: "
            + ", ".join(f"`{c.get('name')}` ({c.get('exception_count')})" for c in worst)
            + "."
        )
    return "\n".join(lines)


def build(report: Mapping[str, Any], *, key: str | None = None) -> dict[str, Any]:
    """Return a report source built from a parsed health report."""
    summary = report.get("summary") or {}
    checks = list(report.get("checks") or [])
    total = int(summary.get("checks_total") or len(checks))
    passed = int(summary.get("checks_passed") or 0)

    status_counts = Counter(str(c.get("status") or "unknown") for c in checks)
    part_rows: list[dict[str, Any]] = []
    parts = summary.get("parts") or {}
    for part in parts or sorted({str(c.get("part")) for c in checks}):
        per = Counter(str(c.get("status")) for c in checks if str(c.get("part")) == part)
        for status in _STATUSES:
            if per.get(status):
                part_rows.append({"part": part, "status": status, "checks": per[status]})

    check_rows = []
    metric_rows = []
    table_exc: dict[str, dict[str, int]] = defaultdict(lambda: {"metrics": 0, "exceptions": 0})
    for c in checks:
        check_rows.append(
            {
                "check_id": c.get("id"),
                "check_name": c.get("name"),
                "part": c.get("part"),
                "edc_table": c.get("edc_table") or "",
                "description": c.get("description") or "",
                "status": c.get("status"),
                "exception_count": int(c.get("exception_count") or 0),
                "metric_count": int(c.get("metric_count") or 0),
                "error": c.get("error") or "",
                "interpretation": c.get("doc_interpretation") or "",
            }
        )
        for m in c.get("metrics") or []:
            src = m.get("source_table") or "(cross-table)"
            flag = bool(m.get("exception_flag"))
            metric_rows.append(
                {
                    "check_id": c.get("id"),
                    "check_name": c.get("name"),
                    "part": c.get("part"),
                    "source_table": src,
                    "metric": m.get("metric"),
                    "value_num": m.get("value_num"),
                    "value_str": m.get("value_str"),
                    "threshold_num": m.get("threshold_num"),
                    "exception": flag,
                    "status": "exception" if flag else "passed",
                }
            )
            table_exc[src]["metrics"] += 1
            table_exc[src]["exceptions"] += int(flag)

    status_ds = dataset(
        "Checks by status",
        [column("status", "nominal", "Status"), column("checks", "quantitative", "Checks")],
        [{"status": s, "checks": status_counts[s]} for s in _STATUSES if status_counts.get(s)],
    )
    metric_cols = [
        column("check_id", "nominal", "Check id"),
        column("check_name", "nominal", "Check"),
        column("part", "nominal", "Part"),
        column("source_table", "nominal", "Source table"),
        column("metric", "nominal", "Metric"),
        column("value_num", "quantitative", "Value"),
        column("value_str", "text", "Value (text)", sensitivity="row_values"),
        column("threshold_num", "quantitative", "Threshold"),
        column("exception", "boolean", "Exception"),
        column("status", "nominal", "Status"),
    ]
    datasets = {
        "summary": dataset(
            "Headline counts",
            [
                column("checks_total", "quantitative", "Checks run"),
                column("checks_passed", "quantitative", "Passed"),
                column("checks_exception", "quantitative", "Exceptions"),
                column("checks_error", "quantitative", "Errors"),
                column("checks_not_applicable", "quantitative", "Not applicable"),
                column("pass_rate", "quantitative", "Pass rate (%)"),
            ],
            [
                {
                    "checks_total": total,
                    "checks_passed": passed,
                    "checks_exception": int(summary.get("checks_exception") or 0),
                    "checks_error": int(summary.get("checks_error") or 0),
                    "checks_not_applicable": int(summary.get("checks_not_applicable") or 0),
                    "pass_rate": round(100.0 * passed / total, 2) if total else 0.0,
                }
            ],
        ),
        "status": status_ds,
        "parts": dataset(
            "Checks by part and status",
            [
                column("part", "ordinal", "Part"),
                column("status", "nominal", "Status"),
                column("checks", "quantitative", "Checks"),
            ],
            part_rows,
        ),
        "checks": dataset(
            "Check outcomes",
            [
                column("check_id", "nominal", "Check id"),
                column("check_name", "nominal", "Check"),
                column("part", "ordinal", "Part"),
                column("edc_table", "nominal", "EDC table"),
                column("description", "text", "Description"),
                column("status", "nominal", "Status"),
                column("exception_count", "quantitative", "Exceptions"),
                column("metric_count", "quantitative", "Metrics"),
                column("error", "text", "Error"),
                column("interpretation", "text", "Interpretation"),
            ],
            check_rows,
        ),
        "metrics": dataset("Check metrics", metric_cols, metric_rows),
        "exceptions": dataset(
            "Exception metrics", metric_cols, [r for r in metric_rows if r["exception"]]
        ),
        "by_table": dataset(
            "Exceptions by source table",
            [
                column("source_table", "nominal", "Source table"),
                column("metrics", "quantitative", "Metrics"),
                column("exceptions", "quantitative", "Exceptions"),
            ],
            [{"source_table": k, **v} for k, v in sorted(table_exc.items())],
        ),
    }
    instance_id = report.get("instance_id")
    return make_source(
        kind="health",
        key=key or str(instance_id or "unknown"),
        title=f"Data health — {report.get('pipeline') or 'run'} {report.get('batch_id') or ''}".strip(),
        datasets=datasets,
        texts={"overview": _overview(report, summary)},
        instance_id=instance_id,
        pipeline=report.get("pipeline"),
        batch_id=report.get("batch_id"),
        cdm=report.get("cdm"),
        generated_at=report.get("generated_at"),
        provenance={
            k: report.get(k)
            for k in (
                "input_address",
                "output_address",
                "results_uri",
                "datamart_id",
                "refresh_date",
            )
            if report.get(k)
        },
        templates=["health.edc_summary", "health.quality_deep_dive"],
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
