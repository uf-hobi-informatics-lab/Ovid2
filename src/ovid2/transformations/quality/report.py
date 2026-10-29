"""Per-run data-health report artifact.

Serialises the check outcomes into the compact JSON artifact
(``health-report-<instance_id>.json``) that the web tier reads to render
the clickable result viewer. The artifact file is the sanctioned hand-off
between the pipeline process and the web tier (like the JSONL event log);
the web tier never imports pipeline code.

Shape validated by ``resources/schemas/health_report.v1.json``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.transformations.quality.results import CheckOutcome

REPORT_VERSION = 1


def report_path(work_dir: Path, instance_id: str) -> Path:
    """Return the artifact path for ``instance_id`` under ``work_dir``."""
    return work_dir / f"health-report-{instance_id}.json"


def build_report(
    *,
    instance_id: str,
    pipeline: str,
    batch_id: str,
    cdm: str,
    input_address: str,
    output_address: str,
    outcomes: list[CheckOutcome],
    results_uri: str | None = None,
    datamart_id: str | None = None,
    refresh_date: str | None = None,
) -> dict[str, Any]:
    """Build the report dict from the check outcomes."""
    parts: dict[str, dict[str, int]] = {}
    passed = exception = error = skipped = not_applicable = 0
    for outcome in outcomes:
        bucket = parts.setdefault(
            outcome.part,
            {"total": 0, "passed": 0, "exception": 0, "error": 0, "not_applicable": 0},
        )
        bucket["total"] += 1
        if outcome.status == "passed":
            passed += 1
            bucket["passed"] += 1
        elif outcome.status == "exception":
            exception += 1
            bucket["exception"] += 1
        elif outcome.status == "error":
            error += 1
            bucket["error"] += 1
        elif outcome.status == "not_applicable":
            not_applicable += 1
            bucket["not_applicable"] += 1
        else:
            skipped += 1
    return {
        "v": REPORT_VERSION,
        "instance_id": instance_id,
        "pipeline": pipeline,
        "batch_id": batch_id,
        "cdm": cdm,
        "input_address": input_address,
        "output_address": output_address,
        "datamart_id": datamart_id,
        "refresh_date": refresh_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "results_uri": results_uri,
        "summary": {
            "checks_total": len(outcomes),
            "checks_passed": passed,
            "checks_exception": exception,
            "checks_error": error,
            "checks_skipped": skipped,
            "checks_not_applicable": not_applicable,
            "parts": parts,
        },
        "checks": [outcome.as_report_dict() for outcome in outcomes],
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Write ``report`` to ``path`` atomically at mode 0o644."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


__all__ = ["REPORT_VERSION", "build_report", "report_path", "write_report"]
