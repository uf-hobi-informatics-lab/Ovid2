"""Doctor / health service.

Thin adapter around :func:`ovid2.installer.doctor.run_all_checks` that
converts :class:`CheckResult` dataclasses into the Pydantic
:class:`ovid2.web.models.HealthResponse` used by both the HTML page and
the JSON API.
"""

from __future__ import annotations

from ovid2.installer import doctor as _doctor
from ovid2.web.models import CheckReport, HealthResponse


def collect_health(image_ref: str | None = None) -> HealthResponse:
    """Run every doctor check and return an aggregate report.

    Args:
        image_ref: Optional Docker image tag override, forwarded to
            :func:`ovid2.installer.doctor.run_all_checks`.

    Returns:
        A :class:`HealthResponse` with the highest severity and one
        :class:`CheckReport` per finding.
    """
    results = _doctor.run_all_checks(image_ref=image_ref)
    worst = _doctor._highest_severity(results)
    checks = [
        CheckReport(name=r.name, severity=r.severity, message=r.message, hint=r.hint)
        for r in results
    ]
    return HealthResponse(worst=worst, checks=checks)
