"""Report sources service (web tier) — durable, aggregated run datasets.

A report source (``report_source.v1.json``) is the input of every Report
Studio report. This service:

- **ingests** run artifacts into sources, using the tier-neutral adapters
  in :mod:`ovid2.reports.sources` (no pipeline imports):
  ``health-report-<id>.json`` (health / dq_checks), ``analysis-<id>.jsonl``
  (analysis transformers) and the run's event-log timeline (run summary);
- **persists** each source as a 0600 JSON file under
  ``$XDG_STATE_HOME/ovid2/reports/sources/`` (``WORK_DIR`` is tmpfs — the
  artifacts would otherwise vanish on reboot) and a catalogue row in
  ``web_report_sources``;
- **describes** sources for the LLM planner without rows
  (:func:`describe`), redacting ``row_values`` columns for agents.

Ingest is idempotent (keyed by ``source_id``) and runs automatically when
the worker finalises a job (:func:`ingest_for_instance`) or on demand via
``POST /api/v1/report-sources/ingest``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.config import paths as xdg_paths
from ovid2.reports.artifacts import analysis_artifact_path, read_analysis_records
from ovid2.reports.sources import ADAPTERS, SourceError, describe_source, validate_source
from ovid2.web import db as web_db

logger = logging.getLogger(__name__)

__all__ = [
    "SourceNotFound",
    "build_trend",
    "delete_orphan_files",
    "describe",
    "get_source",
    "ingest_for_instance",
    "list_kinds",
    "list_sources",
    "sources_dir",
    "store_source",
]

_INSTANCE_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}-[A-Za-z0-9_.-]{1,80}$")


class SourceNotFound(LookupError):
    """Raised when a report source id is unknown."""


def sources_dir() -> Path:
    """Return the durable source store (mode 0700)."""
    override = os.environ.get("OVID2_REPORTS_DIR")
    base = Path(override) if override else xdg_paths.state_dir() / "reports"
    path = base / "sources"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "datasets": {k: len(v.get("rows") or []) for k, v in doc["datasets"].items()},
        "recommended_templates": doc.get("recommended_templates") or [],
    }


def store_source(doc: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Validate and persist a source (replacing any earlier copy)."""
    validate_source(doc)
    sid = doc["source_id"]
    if not _SOURCE_ID_RE.match(sid):
        raise SourceError(f"bad source id {sid!r}")
    file_name = f"{sid}.json"
    path = sources_dir() / file_name
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_report_sources (source_id, kind, title, instance_id, pipeline, batch_id, cdm, "
            "generated_at, ingested_at, ingested_by, file_name, summary_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind, title=excluded.title, "
            "instance_id=excluded.instance_id, pipeline=excluded.pipeline, batch_id=excluded.batch_id, "
            "cdm=excluded.cdm, generated_at=excluded.generated_at, ingested_at=excluded.ingested_at, "
            "ingested_by=excluded.ingested_by, file_name=excluded.file_name, summary_json=excluded.summary_json",
            (
                sid,
                doc["kind"],
                doc["title"],
                doc.get("instance_id"),
                doc.get("pipeline"),
                doc.get("batch_id"),
                doc.get("cdm"),
                doc.get("generated_at"),
                _now(),
                actor,
                file_name,
                json.dumps(_summary(doc)),
            ),
        )
    return _row_dict_for(sid)


def _row_to_dict(row: Any) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys() if k not in ("summary_json", "file_name")}  # noqa: SIM118 sqlite3.Row
    with contextlib.suppress(ValueError):
        out.update(json.loads(row["summary_json"] or "{}"))
    return out


def _row_dict_for(source_id: str) -> dict[str, Any]:
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_report_sources WHERE source_id = ?", (source_id,)
        ).fetchone()
    if row is None:
        raise SourceNotFound(source_id)
    return _row_to_dict(row)


def list_kinds() -> list[dict[str, str]]:
    """Return the registered source kinds."""
    return [
        {"kind": a.kind, "label": a.label, "description": a.description} for a in ADAPTERS.values()
    ]


def list_sources(
    *,
    kind: str | None = None,
    pipeline: str | None = None,
    instance_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a page of the source catalogue (newest first)."""
    web_db.ensure_schema()
    like = f"%{q.lower()}%" if q else None
    params: list[Any] = [
        kind,
        kind,
        pipeline,
        pipeline,
        instance_id,
        instance_id,
        like,
        like,
        like,
        like,
    ]
    limit = max(1, min(int(limit), 500))
    with web_db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM web_report_sources WHERE " + _LIST_WHERE, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM web_report_sources WHERE "
            + _LIST_WHERE
            + " ORDER BY coalesce(generated_at, ingested_at) DESC LIMIT ? OFFSET ?",
            [*params, limit, max(0, int(offset))],
        ).fetchall()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "sources": [_row_to_dict(r) for r in rows],
    }


#: Fixed WHERE clause; every filter is optional via ``? IS NULL``.
_LIST_WHERE = (
    "(? IS NULL OR kind = ?) AND (? IS NULL OR pipeline = ?) AND (? IS NULL OR instance_id = ?) "
    "AND (? IS NULL OR lower(title) LIKE ? OR lower(coalesce(batch_id, '')) LIKE ? OR source_id LIKE ?)"
)


def get_source(source_id: str) -> dict[str, Any]:
    """Return the full source document (with rows)."""
    if not _SOURCE_ID_RE.match(source_id or ""):
        raise SourceNotFound(source_id)
    path = sources_dir() / f"{source_id}.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SourceNotFound(source_id) from exc
    if not isinstance(doc, dict):
        raise SourceNotFound(source_id)
    return doc


def describe(source_id: str, *, redact_row_values: bool) -> dict[str, Any]:
    """Return the row-free description of a source (what the LLM reads)."""
    out = describe_source(get_source(source_id), redact_row_values=redact_row_values)
    out["row_values_redacted"] = redact_row_values
    return out


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _work_dir() -> Path:
    from ovid2.config.settings import WORK_DIR

    return Path(WORK_DIR)


def _ingest_health(instance_id: str, actor: str) -> dict[str, Any] | None:
    from ovid2.web.services import health_report_svc

    report = health_report_svc.get_report(instance_id)
    if report is None:
        return None
    return store_source(ADAPTERS["health"].build(report, key=instance_id), actor=actor)


def _ingest_analysis(instance_id: str, actor: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    path = analysis_artifact_path(instance_id, _work_dir())
    if not path.is_file():
        return None
    records = read_analysis_records(path)
    if not records:
        return None
    doc = ADAPTERS["analysis"].build(
        records,
        instance_id=instance_id,
        pipeline=meta.get("pipeline"),
        batch_id=meta.get("batch_id"),
    )
    return store_source(doc, actor=actor)


def _ingest_run_summary(
    instance_id: str, actor: str, meta: dict[str, Any]
) -> dict[str, Any] | None:
    from ovid2.web.services import events_svc, timeline_svc

    if not events_svc.event_log_path(instance_id).is_file():
        return None
    timeline = timeline_svc.build_timeline(instance_id).model_dump()
    if not timeline.get("events_seen"):
        return None
    doc = ADAPTERS["run_summary"].build(
        timeline,
        pipeline=meta.get("pipeline"),
        batch_id=meta.get("batch_id"),
        run_kind=meta.get("run_kind"),
    )
    return store_source(doc, actor=actor)


def ingest_for_instance(
    instance_id: str,
    *,
    actor: str = "worker",
    pipeline: str | None = None,
    batch_id: str | None = None,
    run_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Ingest every report source a run produced; return the stored rows.

    Never raises for a missing artifact — each producer is optional.
    """
    if not _INSTANCE_RE.match(instance_id or ""):
        raise SourceNotFound(instance_id)
    if pipeline is None or batch_id is None or run_kind is None:
        from ovid2.web.services import jobs_svc

        job = jobs_svc.get_job_by_instance(instance_id)
        if job is not None:
            pipeline = pipeline or job.pipeline
            batch_id = batch_id or job.batch_id
            run_kind = run_kind or str(job.extra_args.get("kind") or "etl")
    meta = {"pipeline": pipeline, "batch_id": batch_id, "run_kind": run_kind}
    stored: list[dict[str, Any]] = []
    steps: tuple[Callable[[], dict[str, Any] | None], ...] = (
        lambda: _ingest_health(instance_id, actor),
        lambda: _ingest_analysis(instance_id, actor, meta),
        lambda: _ingest_run_summary(instance_id, actor, meta),
    )
    for fn in steps:
        try:
            row = fn()
        except (SourceError, OSError, ValueError, KeyError) as exc:
            logger.warning("report-source ingest for %s failed: %s", instance_id, exc)
            continue
        if row is not None:
            stored.append(row)
    return stored


def build_trend(source_ids: list[str], *, actor: str) -> dict[str, Any]:
    """Build and store a ``health_trend`` source from 2+ health sources."""
    docs = [get_source(s) for s in source_ids]
    return store_source(ADAPTERS["health_trend"].build(docs), actor=actor)


def delete_orphan_files() -> int:
    """Remove source files with no catalogue row (housekeeping)."""
    with web_db.connect() as conn:
        known = {r[0] for r in conn.execute("SELECT file_name FROM web_report_sources").fetchall()}
    removed = 0
    for path in sources_dir().glob("*.json"):
        if path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the ovid2 web services.")
