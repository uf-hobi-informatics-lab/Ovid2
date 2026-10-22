"""Run / batch listing service.

Reads the canonical batch state from
:class:`ovid2.scheduler.batch_tracker.SqliteBatchTracker`'s ``data_batch``
table (the scheduler writes here from both the daemon and the ETL
pipeline). Where a row was submitted through the web tier, we augment
it with columns from ``web_jobs``.

Snowflake-backed installations query the equivalent
``OVID.SCHEDULER.data_batch`` table; that variant lands in W3 alongside
the submit path.
"""

from __future__ import annotations

import sqlite3

from ovid2.scheduler.batch_tracker import _default_sqlite_path
from ovid2.web import db as web_db
from ovid2.web.models import RunListResponse, RunSummary


def _report_available(instance_id: str | None) -> bool:
    """Return whether a data-health report artifact exists for a run."""
    if not instance_id:
        return False
    from ovid2.web.services import health_report_svc

    return health_report_svc.report_available(instance_id)


def _tracker_db_available() -> bool:
    """Return True if the SQLite scheduler DB has a ``data_batch`` table.

    The web-tier DB shares the same file when the scheduler runs on
    SQLite, so file existence is not enough — we probe for the table.
    """
    path = _default_sqlite_path()
    if not path.is_file():
        return False
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'data_batch'"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _load_web_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every web_jobs row, newest submission first.

    One row per job — each submission and each rerun is its own run in
    the merged workspace (the old Jobs page semantics). Unlike
    :func:`_load_web_jobs_by_batch`, this does not collapse jobs that
    share a ``batch_id``.
    """
    cursor = conn.execute(
        "SELECT job_id, pipeline, batch_id, status, instance_id, submitted_by, submitted_at "
        "FROM web_jobs ORDER BY submitted_at DESC"
    )
    return list(cursor.fetchall())


def _load_web_jobs_by_batch(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Return web_jobs rows keyed by ``batch_id`` (most-recent submission wins)."""
    latest: dict[str, sqlite3.Row] = {}
    for row in _load_web_jobs(conn):
        latest.setdefault(row["batch_id"], row)
    return latest


def list_runs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    pipeline: str | None = None,
) -> RunListResponse:
    """Return a paginated slice of the run history — one row per run.

    A "run" is a single job submission (or rerun): the list is job-first
    (the old Jobs-page semantics), so multiple jobs that share a
    ``batch_id`` each get their own row rather than collapsing into one.
    Each web job is enriched from the scheduler's ``data_batch`` row for
    the same ``batch_id`` when one exists (batch-level status / path /
    processing timestamps). Scheduler batches that have no web job at all
    (e.g. runs launched outside the web tier) are surfaced as their own
    rows too.

    Args:
        limit: Maximum rows to return (clamped to 500).
        offset: Row offset into the newest-first stream.
        status: Optional ``data_batch``-style status filter (matched
            against the run's effective status, e.g. ``PROCESSED``).
        pipeline: Optional pipeline filter (matches ``web_jobs.pipeline``).

    Returns:
        A :class:`RunListResponse`. When neither web jobs nor scheduler
        batches exist (fresh install), ``total=0`` with an empty list.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    web_db.ensure_schema()
    with web_db.connect() as conn:
        web_jobs = _load_web_jobs(conn)

    # Tracker batch rows keyed by batch_id, for enrichment + orphan runs.
    batches: dict[str, sqlite3.Row] = {}
    if _tracker_db_available():
        conn = sqlite3.connect(_default_sqlite_path())
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            for row in conn.execute(
                "SELECT batch_id, path, received_time, status, "
                "processing_start, processing_end FROM data_batch"
            ).fetchall():
                # Most-recent write wins if a batch_id appears twice.
                batches.setdefault(row["batch_id"], row)
        finally:
            conn.close()

    rows: list[RunSummary] = []
    seen_batches: set[str] = set()

    # (a) One row per web job, enriched from its batch row when present.
    for job in web_jobs:
        batch = batches.get(job["batch_id"])
        if batch is not None:
            seen_batches.add(job["batch_id"])
        rows.append(_row_from_web_job(job, batch))

    # (b) Scheduler batches with no web job at all — surface them so the
    # workspace is a complete run history, not just web submissions.
    for batch_id, batch in batches.items():
        if batch_id in seen_batches:
            continue
        rows.append(_row_from_batch(batch))

    # Normalise the filters: an empty (or whitespace-only) value means
    # "no filter". The runs page submits a GET form, so an unselected
    # status dropdown ("any status") and an empty pipeline box arrive as
    # empty strings — treating those as active filters would compare every
    # row against "" and drop them all.
    status_filter = (status or "").strip()
    pipeline_filter = (pipeline or "").strip().casefold()

    # Filter, then order newest-first over the whole union.
    def _keep(run: RunSummary) -> bool:
        if pipeline_filter and pipeline_filter not in (run.pipeline or "").casefold():
            return False
        return not (status_filter and (run.status or "") != status_filter)

    rows = [r for r in rows if _keep(r)]
    rows.sort(key=lambda r: r.received_time or "", reverse=True)

    total = len(rows)
    return RunListResponse(
        total=total,
        limit=limit,
        offset=offset,
        runs=rows[offset : offset + limit],
    )


def get_run(identifier: str) -> RunSummary | None:
    """Return a single run by ``batch_id`` or ``instance_id``.

    The merged workspace links rows by ``instance_id`` (unique per job)
    but the scheduler / older links use ``batch_id``; resolve either.
    """
    response = list_runs(limit=500, offset=0)
    for run in response.runs:
        if run.batch_id == identifier or run.instance_id == identifier:
            return run
    # Fall back to a targeted batch lookup in case pagination missed it.
    if not _tracker_db_available():
        return None
    conn = sqlite3.connect(_default_sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT batch_id, path, received_time, status, "
            "processing_start, processing_end "
            "FROM data_batch WHERE batch_id = ?",
            (identifier,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_from_batch(row)


def _row_from_web_job(job: sqlite3.Row, batch: sqlite3.Row | None = None) -> RunSummary:
    """Convert a web_jobs row into a :class:`RunSummary`.

    When a scheduler ``data_batch`` row exists for the same ``batch_id``
    (``batch``), enrich the run with its path and processing timestamps.
    The status still comes from the web job so each submission / rerun
    reflects its own outcome (queued / running / done / failed /
    cancelled) rather than the shared batch's latest state.
    """
    return RunSummary(
        batch_id=job["batch_id"],
        path=batch["path"] if batch is not None else None,
        status=_status_from_web_status(job["status"]),
        received_time=job["submitted_at"],
        processing_start=batch["processing_start"] if batch is not None else None,
        processing_end=batch["processing_end"] if batch is not None else None,
        job_id=job["job_id"],
        instance_id=job["instance_id"],
        pipeline=job["pipeline"],
        submitted_by=job["submitted_by"],
        report_available=_report_available(job["instance_id"]),
    )


def _row_from_batch(batch: sqlite3.Row) -> RunSummary:
    """Convert a scheduler ``data_batch`` row (no web job) into a run row."""
    return RunSummary(
        batch_id=batch["batch_id"],
        path=batch["path"],
        status=batch["status"],
        received_time=batch["received_time"],
        processing_start=batch["processing_start"],
        processing_end=batch["processing_end"],
    )


def _status_from_web_status(status: str) -> str:
    """Map a ``web_jobs.status`` value onto a ``data_batch``-style label."""
    return {
        "draft": "DRAFT",
        "queued": "NEW",
        "running": "PROCESSING",
        "done": "PROCESSED",
        "failed": "ERROR",
        "cancelled": "CANCELLED",
    }.get(status, status.upper())


def list_badge_for_web_status(status: str) -> str:
    """Public alias of the run-list badge mapping (used by the badge poll)."""
    return _status_from_web_status(status)
