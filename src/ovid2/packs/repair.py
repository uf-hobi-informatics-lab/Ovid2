"""Repair context: failed health checks + remediation hints (tier-neutral).

Pure function over a parsed health report (``health_report.v1.json``)
and the check catalogue. Powers ``GET /api/v1/runs/{id}/repair-context``
and the assistant's ``get_repair_context`` tool. The payload is
**aggregate only**: check ids, severities, counts, thresholds and the
pack-authored hint. Metric ``value_str`` / ``details`` (which can echo
source values) are dropped unless the caller is allowed to see row
values.

The assistant reads this to *propose* a fix through the existing
PROPOSE tools (transform chains, fixer configs, re-runs). Nothing here
applies a change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["SEVERITY_ORDER", "build_repair_context"]

#: Severity rank (lower = more urgent).
SEVERITY_ORDER: dict[str, int] = {"blocker": 0, "warning": 1, "info": 2}

_FAILED = ("exception", "error")


def build_repair_context(
    report: Mapping[str, Any],
    catalogue: Mapping[str, Mapping[str, Any]],
    *,
    include_row_values: bool = False,
    max_metrics: int = 20,
) -> dict[str, Any]:
    """Return the repair context for one health run.

    Args:
        report: Parsed ``health-report-<id>.json``.
        catalogue: ``check_id -> catalogue entry`` (``severity``,
            ``remediation``, ``pack`` …) — the web-safe check list.
        include_row_values: Keep ``value_str`` / ``details`` on metrics.
            Callers pass ``True`` only for humans with
            ``VIEW_PIPELINE_DATA`` (never for agents).
        max_metrics: Cap on exception metrics listed per check.

    Returns:
        ``{instance_id, pipeline, cdm, summary, failures: [...]}`` with
        failures ordered by severity, then exception count (desc).
    """
    failures: list[dict[str, Any]] = []
    for check in report.get("checks") or []:
        status = str(check.get("status") or "")
        if status not in _FAILED:
            continue
        cid = str(check.get("id") or "")
        meta = catalogue.get(cid) or {}
        severity = str(check.get("severity") or meta.get("severity") or "warning")
        remediation = dict(meta.get("remediation") or {})
        metrics: list[dict[str, Any]] = []
        for m in check.get("metrics") or []:
            if not m.get("exception_flag"):
                continue
            row: dict[str, Any] = {
                "source_table": m.get("source_table"),
                "metric": m.get("metric"),
                "value_num": m.get("value_num"),
                "threshold_num": m.get("threshold_num"),
            }
            if include_row_values:
                row["value_str"] = m.get("value_str")
                row["details"] = m.get("details")
            metrics.append(row)
            if len(metrics) >= max_metrics:
                break
        failures.append(
            {
                "check_id": cid,
                "name": check.get("name"),
                "part": check.get("part"),
                "pack": check.get("pack") or meta.get("pack") or "",
                "status": status,
                "severity": severity,
                "description": check.get("description") or meta.get("description") or "",
                "interpretation": check.get("doc_interpretation") or "",
                "exception_count": int(check.get("exception_count") or 0),
                "error": check.get("error") if status == "error" else None,
                "metrics": metrics,
                "remediation": {
                    "hint": str(remediation.get("hint") or ""),
                    "fixers": [str(f) for f in remediation.get("fixers") or []],
                    "transforms": [str(t) for t in remediation.get("transforms") or []],
                    "docs": str(remediation.get("docs") or ""),
                },
            }
        )
    failures.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), -int(f["exception_count"])))
    by_severity: dict[str, int] = {}
    for f in failures:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    return {
        "instance_id": report.get("instance_id"),
        "pipeline": report.get("pipeline"),
        "batch_id": report.get("batch_id"),
        "cdm": report.get("cdm"),
        "summary": {
            "failures": len(failures),
            "by_severity": by_severity,
            "with_remediation": sum(
                1
                for f in failures
                if f["remediation"]["hint"]
                or f["remediation"]["fixers"]
                or f["remediation"]["transforms"]
            ),
        },
        "row_values_included": include_row_values,
        "failures": failures,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.packs.repair is a library module.")
