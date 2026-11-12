"""Report-source adapters (the generic source contract).

A *report source* is a durable bundle of small named datasets
(``report_source.v1.json``). Each producer kind registers an adapter in
:data:`ADAPTERS`; an adapter is a pure function from the producer's
artifact (already-parsed JSON / event aggregates) to a source document.

Adding a source kind:

1. write ``ovid2/reports/sources/<kind>.py`` exposing
   ``build(<artifact>, **meta) -> dict``;
2. register it in :data:`ADAPTERS` with a label + description;
3. ship at least one template under ``resources/reports/templates/<kind>/``.

``tests/contract/test_report_catalogues.py`` checks every adapter has a
template and that every template validates against a sample source.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from ovid2.reports.catalogue import load_schema
from ovid2.reports.data import MAX_DATASET_ROWS

__all__ = [
    "ADAPTERS",
    "SourceAdapter",
    "SourceError",
    "column",
    "dataset",
    "describe_source",
    "make_source",
    "source_id_for",
    "validate_source",
]


class SourceError(ValueError):
    """Raised when a report source cannot be built or fails validation."""


@dataclass(frozen=True)
class SourceAdapter:
    """Registry entry for one source kind."""

    kind: str
    label: str
    description: str
    build: Callable[..., dict[str, Any]]


def column(
    name: str,
    ctype: str,
    title: str | None = None,
    *,
    sensitivity: str = "aggregate",
    is_count: bool = False,
) -> dict[str, Any]:
    """Return a dataset column descriptor."""
    out: dict[str, Any] = {"name": name, "type": ctype, "sensitivity": sensitivity}
    if title:
        out["title"] = title
    if is_count:
        out["is_count"] = True
    return out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def dataset(
    title: str,
    columns: list[dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    description: str = "",
) -> dict[str, Any]:
    """Return a dataset, projecting rows onto ``columns`` and capping size."""
    names = [c["name"] for c in columns]
    capped = list(rows[:MAX_DATASET_ROWS])
    out: dict[str, Any] = {
        "title": title,
        "columns": columns,
        "rows": [{n: _jsonable(r.get(n)) for n in names} for r in capped],
    }
    if description:
        out["description"] = description
    if len(rows) > MAX_DATASET_ROWS:
        out["truncated"] = True
    return out


def source_id_for(kind: str, key: str) -> str:
    """Return the canonical source id ``<kind>-<key>``."""
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in key)[:80] or "x"
    return f"{kind}-{safe}"


def make_source(
    *,
    kind: str,
    key: str,
    title: str,
    datasets: dict[str, dict[str, Any]],
    texts: dict[str, str] | None = None,
    instance_id: str | None = None,
    pipeline: str | None = None,
    batch_id: str | None = None,
    cdm: str | None = None,
    generated_at: str | None = None,
    provenance: dict[str, Any] | None = None,
    templates: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble and validate a report-source document."""
    doc: dict[str, Any] = {
        "v": 1,
        "source_id": source_id_for(kind, key),
        "kind": kind,
        "title": title[:200] or kind,
        "instance_id": instance_id,
        "pipeline": pipeline,
        "batch_id": batch_id,
        "cdm": cdm,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "provenance": provenance or {},
        "recommended_templates": templates or [],
        "texts": texts or {},
        "datasets": datasets,
    }
    validate_source(doc)
    return doc


def validate_source(doc: Any) -> None:
    """Raise :class:`SourceError` unless ``doc`` is a valid report source."""
    validator = Draft202012Validator(load_schema("report_source.v1"))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = ["/".join(str(p) for p in e.absolute_path) + ": " + e.message for e in errors[:5]]
        raise SourceError("; ".join(msgs))


def describe_source(doc: Mapping[str, Any], *, redact_row_values: bool = False) -> dict[str, Any]:
    """Return a schema-level description of a source (no rows).

    Per column: type, sensitivity, distinct count and — for aggregate
    columns only — min / max (numeric) or up to 12 example values. This
    is what the LLM planner reads; ``row_values`` columns never expose
    examples.
    """
    out: dict[str, Any] = {
        k: doc.get(k)
        for k in (
            "source_id",
            "kind",
            "title",
            "instance_id",
            "pipeline",
            "batch_id",
            "cdm",
            "generated_at",
            "recommended_templates",
        )
    }
    out["texts"] = sorted((doc.get("texts") or {}).keys())
    datasets: dict[str, Any] = {}
    for name, ds in (doc.get("datasets") or {}).items():
        rows = ds.get("rows") or []
        cols = []
        for c in ds.get("columns") or []:
            values = [r.get(c["name"]) for r in rows if r.get(c["name"]) is not None]
            info: dict[str, Any] = dict(c)
            info["distinct"] = len({json.dumps(v, sort_keys=True, default=str) for v in values})
            hidden = c.get("sensitivity") == "row_values" and redact_row_values
            if not hidden:
                nums = [
                    v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
                ]
                if c.get("type") == "quantitative" and nums:
                    info["min"] = min(nums)
                    info["max"] = max(nums)
                elif c.get("type") != "quantitative":
                    seen: list[Any] = []
                    for v in values:
                        if v not in seen:
                            seen.append(v)
                        if len(seen) >= 12:
                            break
                    info["examples"] = seen
            cols.append(info)
        datasets[name] = {
            "title": ds.get("title"),
            "description": ds.get("description", ""),
            "row_count": len(rows),
            "truncated": bool(ds.get("truncated")),
            "columns": cols,
        }
    out["datasets"] = datasets
    return out


def _registry() -> dict[str, SourceAdapter]:
    from ovid2.reports.sources import analysis, health, health_trend, run_summary

    return {
        "health": SourceAdapter(
            "health",
            "Data-health / EDC checks",
            "Check outcomes, per-part summaries and metric rows from an `ovid2 health` / `dq_checks` run.",
            health.build,
        ),
        "analysis": SourceAdapter(
            "analysis",
            "Analysis profile",
            "Monthly record volumes and value frequencies from an `ovid2 analysis` run.",
            analysis.build,
        ),
        "run_summary": SourceAdapter(
            "run_summary",
            "Run summary",
            "Stage, table and fix outcomes and timings aggregated from any run's event log.",
            run_summary.build,
        ),
        "health_trend": SourceAdapter(
            "health_trend",
            "Data-health trend",
            "Data-health results across several runs of one pipeline (refresh-over-refresh).",
            health_trend.build,
        ),
    }


ADAPTERS: dict[str, SourceAdapter] = _registry()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
