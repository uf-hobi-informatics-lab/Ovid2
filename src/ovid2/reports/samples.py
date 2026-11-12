"""Synthetic, site-neutral sample artifacts for every report-source kind.

Used by ``tests/contract/test_report_catalogues.py`` (every adapter has a
template and every template validates + renders against a sample source)
and by the unit suites. Values are invented; nothing here is real data.
"""

from __future__ import annotations

from typing import Any

__all__ = ["health_report", "sample_sources"]


def health_report(instance_id: str = "20260101-000000-aaaaa1", passed: int = 1) -> dict[str, Any]:
    """Return a minimal ``health-report-<id>.json`` document."""
    rows = [
        ("exception", "part1", "DEMOGRAPHIC"),
        ("passed", "part1", "ENCOUNTER"),
        ("error", "part2", "DIAGNOSIS"),
        ("passed", "part3", "LAB_RESULT_CM"),
        ("exception", "mapping", "PROCEDURES"),
    ]
    checks = []
    for i, (status, part, table) in enumerate(rows):
        checks.append(
            {
                "id": f"1.0{i}",
                "name": f"DC_1_0{i}",
                "part": part,
                "edc_table": "Table IA",
                "description": "Synthetic check",
                "status": status,
                "exception_count": 3 * i if status == "exception" else 0,
                "metric_count": 2,
                "error": "boom" if status == "error" else None,
                "doc_interpretation": "Synthetic interpretation.",
                "metrics": [
                    {
                        "source_table": table,
                        "metric": "M",
                        "value_num": float(i),
                        "value_str": None,
                        "threshold_num": 1.0,
                        "exception_flag": status == "exception",
                        "details": {},
                    },
                    {
                        "source_table": "HARVEST",
                        "metric": "N",
                        "value_num": 2.0,
                        "value_str": "abc",
                        "threshold_num": None,
                        "exception_flag": False,
                        "details": {},
                    },
                ],
            }
        )
    return {
        "v": 1,
        "instance_id": instance_id,
        "pipeline": "demo",
        "batch_id": "demo/b" + instance_id[-2:],
        "cdm": "pcornet_7_0",
        "generated_at": f"2026-01-0{max(1, min(9, passed))}T00:00:00+00:00",
        "summary": {
            "checks_total": 5,
            "checks_passed": passed,
            "checks_exception": 2,
            "checks_error": 1,
            "checks_skipped": 0,
            "checks_not_applicable": 0,
            "parts": {"part1": {}, "part2": {}, "part3": {}, "mapping": {}},
        },
        "checks": checks,
    }


def analysis_records() -> list[dict[str, Any]]:
    """Return records shaped like ``analysis-<id>.jsonl``."""
    return [
        {
            "type": "monthly",
            "table": "ENCOUNTER",
            "column": "ADMIT_DATE",
            "rows": [{"month": f"2024-{m:02d}", "records": 100 + m * 7} for m in range(1, 13)],
        },
        {
            "type": "monthly",
            "table": "DIAGNOSIS",
            "column": "DX_DATE",
            "rows": [{"month": f"2024-{m:02d}", "records": 300 - m * 5} for m in range(1, 13)],
        },
        {
            "type": "distinct",
            "table": "ENCOUNTER",
            "column": "ENC_TYPE",
            "total_rows": 1500,
            "n_distinct": 4,
            "null_rows": 30,
            "top": [
                {"value": "AV", "records": 900, "percentage": 60.0},
                {"value": "IP", "records": 5, "percentage": 0.3},
            ],
        },
    ]


def run_timeline(instance_id: str = "20260101-000000-cccccc") -> dict[str, Any]:
    """Return a ``RunTimeline``-shaped dict."""
    return {
        "instance_id": instance_id,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:05:00+00:00",
        "success": True,
        "events_seen": 12,
        "warning_count": 1,
        "error_count": 0,
        "stage_counts": {
            "read": {"started": 2, "completed": 2, "failed": 0},
            "fix": {"started": 2, "completed": 1, "failed": 1},
        },
        "tables": [
            {"table": "ENCOUNTER", "status": "completed", "elapsed_ms": 12000},
            {"table": "DIAGNOSIS", "status": "failed", "elapsed_ms": 3000, "error": "boom"},
        ],
        "fixes": [
            {"table": "ENCOUNTER", "fix": "patid_orphans", "status": "applied"},
            {"table": "DIAGNOSIS", "fix": "patid_orphans", "status": "failed", "error": "x"},
        ],
    }


def sample_sources() -> dict[str, dict[str, Any]]:
    """Return one built source per registered kind (``{kind: source}``)."""
    from ovid2.reports.sources import ADAPTERS

    health = ADAPTERS["health"].build(health_report("20260101-000000-aaaaa1"))
    health2 = ADAPTERS["health"].build(health_report("20260102-000000-aaaaa2", passed=2))
    return {
        "health": health,
        "health_trend": ADAPTERS["health_trend"].build([health, health2]),
        "analysis": ADAPTERS["analysis"].build(
            analysis_records(), instance_id="20260101-000000-bbbbbb", pipeline="demo"
        ),
        "run_summary": ADAPTERS["run_summary"].build(run_timeline(), pipeline="demo"),
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
