"""Data-health report service (web tier).

Reads the per-run JSON report artifact
(``health-report-<instance_id>.json``) that the ``ovid2 health`` pipeline
writes beside the event log. The artifact file is the sanctioned hand-off
between the pipeline process and the web tier — this service reads it
directly and **never imports pipeline code** (see
``tests/contract/test_web_no_pipeline_imports.py``).

Not to be confused with :mod:`ovid2.web.services.health_svc`, which wraps
the installer *doctor* checks (system health, not data health).

Powers:

- the "View report →" link on run records (:func:`report_available`),
- the clickable result viewer at ``/runs/{id}/report``,
- the REST endpoint ``GET /api/v1/runs/{id}/report``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Canonical part ordering for the check catalogue.
#:
#: Web-safe mirror of
#: :data:`ovid2.transformations.quality.registry._PART_ORDER` — the web
#: tier is contractually forbidden from importing pipeline code
#: (``tests/contract/test_web_no_pipeline_imports.py``), so the numeric
#: bucket order is duplicated here. It MUST stay in lock-step with the
#: registry so the browser catalogue (and the live-console roster it
#: seeds) lists checks in the same order the runner emits them:
#: ``part1 → … → part4 → mapping → freshness → integrity →
#: completeness → semantic``. Unknown parts sort last (``99``).
_PART_ORDER: dict[str, int] = {
    "part1": 0,
    "part2": 1,
    "part3": 2,
    "part4": 3,
    "mapping": 4,
    "freshness": 5,
    "integrity": 6,
    "completeness": 7,
    "semantic": 8,
}


def _check_sort_key(check: dict[str, Any]) -> tuple[int, tuple[object, ...]]:
    """Return the canonical ``(part-rank, natural-id)`` sort key.

    Orders by the numeric part bucket first (so ``part4`` precedes
    ``mapping`` rather than sorting alphabetically), then by a
    *natural* split of the id so numeric ids collate as numbers
    (``1.2`` before ``1.10``) while the ``health.*`` ids fall back to a
    plain string compare. Matches
    :attr:`ovid2.transformations.quality.registry.CheckSpec.sort_key`.
    """
    part = str(check.get("part", ""))
    rank = _PART_ORDER.get(part, 99)
    return (rank, _natural_id_key(str(check.get("id", ""))))


def _natural_id_key(check_id: str) -> tuple[object, ...]:
    """Split a check id into a mixed number/string tuple for natural sort.

    ``'1.01'`` → ``(1, 1)``; ``'health.orphan_fk'`` →
    ``('health', 'orphan_fk')`` — numeric fragments compare as ints so
    ``1.2`` sorts before ``1.10`` regardless of zero-padding, and each
    fragment is a homogeneous ``(is_str, value)`` pair so ints and
    strings never compare against each other.
    """
    parts: list[tuple[int, object]] = []
    for frag in check_id.split("."):
        if frag.isdigit():
            parts.append((0, int(frag)))
        else:
            parts.append((1, frag))
    return tuple(parts)


def _work_dir() -> Path:
    """Return the current ``WORK_DIR`` via a late import (test-patch safe)."""
    from ovid2.config.settings import WORK_DIR

    return WORK_DIR


def report_path(instance_id: str) -> Path:
    """Return the expected report-artifact path for an instance."""
    return _work_dir() / f"health-report-{instance_id}.json"


def report_available(instance_id: str) -> bool:
    """Return whether a health report artifact exists for ``instance_id``."""
    try:
        return report_path(instance_id).is_file()
    except OSError:
        return False


def get_report(instance_id: str) -> dict[str, Any] | None:
    """Return the parsed report dict, or ``None`` when absent / unreadable."""
    path = report_path(instance_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("health report %s unreadable: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def report_summary(instance_id: str) -> dict[str, Any] | None:
    """Return just the ``summary`` block (for compact list rendering)."""
    report = get_report(instance_id)
    if report is None:
        return None
    summary = report.get("summary")
    return summary if isinstance(summary, dict) else None


def _checks_dir() -> Path:
    """Return the packaged health-check spec directory.

    Resolved relative to the installed ``ovid2`` package so it works
    from a wheel. Reads the YAML specs directly rather than importing
    :mod:`ovid2.transformations.quality.registry` — the web tier is
    contractually forbidden from importing pipeline code
    (``tests/contract/test_web_no_pipeline_imports.py``).
    """
    import ovid2

    root = Path(ovid2.__file__).resolve().parent
    return root / "resources" / "health" / "checks"


def _packages_dir() -> Path:
    """Return the packaged health-check *package* (report-cycle) directory.

    Resolved relative to the installed ``ovid2`` package (wheel-safe) and
    read as YAML directly — same web-tier isolation rule as
    :func:`_checks_dir`.
    """
    import ovid2

    root = Path(ovid2.__file__).resolve().parent
    return root / "resources" / "health" / "packages"


def list_checks() -> list[dict[str, Any]]:
    """Return the registered health-check catalogue (web-safe).

    Reads the packaged YAML specs directly. Each entry carries the
    fields the panel / MCP client needs: ``id``, ``name``, ``part``,
    ``operator``, ``description``, ``edc_table``, ``enabled``.
    """
    import yaml

    checks_dir = _checks_dir()
    if not checks_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(checks_dir.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("health check spec %s unreadable: %s", path, exc)
            continue
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        out.append(
            {
                "id": str(raw.get("id", "")),
                "name": str(raw.get("name", "")),
                "part": str(raw.get("part", "")),
                "operator": str(raw.get("operator", "")),
                "description": str(raw.get("description", "")),
                "edc_table": str(raw.get("edc_table", "")),
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    out.sort(key=_check_sort_key)
    return out


def list_packages() -> list[dict[str, Any]]:
    """Return the registered EDC/DC report-cycle packages (web-safe).

    Reads the packaged package manifests directly. Each entry carries
    ``id``, ``label``, ``released``, ``description`` and the resolved
    ``checks`` list. Ordered most-recent first (by ``released`` date,
    then ``id`` descending) so the first entry is the picker default.
    """
    import yaml

    packages_dir = _packages_dir()
    if not packages_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(packages_dir.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("health package manifest %s unreadable: %s", path, exc)
            continue
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        out.append(
            {
                "id": str(raw.get("id", "")),
                "label": str(raw.get("label", "")),
                "released": str(raw.get("released", "")),
                "description": str(raw.get("description", "")),
                "checks": [str(c) for c in raw.get("checks", [])],
            }
        )
    out.sort(key=lambda p: (p["released"], p["id"]), reverse=True)
    return out


__all__ = [
    "get_report",
    "list_checks",
    "list_packages",
    "report_available",
    "report_path",
    "report_summary",
]
