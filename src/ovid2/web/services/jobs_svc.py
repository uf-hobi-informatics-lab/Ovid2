"""Job queue service — submit / cancel / rerun / status.

Every entry in the ``web_jobs`` table is a request to run
``ovid2 etl …`` in a subprocess. The worker
(:mod:`ovid2.web.worker`) pops queued rows in insertion order and
dispatches them; this module owns every write path onto the table so
routes and CLI callers share the same state machine.

State machine::

    draft   ---(launch, human ▶)-->  queued
    draft   ---(discard)--------->  (row deleted)
    queued  ---(worker picks up)-->  running
    queued  ---(cancel)---------->  cancelled
    running ---(process exit 0)-->  done
    running ---(process exit !=0)->  failed
    running ---(SIGTERM by us)-->   cancelled
    running ---(worker crash)---->  failed  (resumed on next start-up)

Rerun semantics (option (a) from ADR): a new row is inserted with
``parent_job_id`` pointing at the original, the underlying
``ovid2 etl`` invocation is byte-identical, and the scheduler tracker
row for that ``batch_id`` is reset to ``NEW``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ovid2.config import addresses as _addresses
from ovid2.run_kinds import get_kind
from ovid2.scheduler.batch_tracker import (
    SqliteBatchTracker,
    _default_sqlite_path,
)
from ovid2.web import db as web_db
from ovid2.web.models import (
    PACKAGE_FREE_KINDS,
    JobDetail,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    normalize_kind,
)
from ovid2.web.services import pipelines_meta_svc
from ovid2.web.settings import web_log_dir

logger = logging.getLogger(__name__)

STATUS_DRAFT = "draft"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_DRAFT, STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED,
     STATUS_CANCELLED}
)


class SubmitError(ValueError):
    """Raised when a submit request fails validation."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_instance_id() -> str:
    """Return a fresh instance id matching :mod:`ovid2.core.isolation`.

    We deliberately re-implement the format here rather than importing
    ``ovid2.core.isolation`` — the web tier must not import from
    ``ovid2.core`` (see ``tests/contract/test_web_no_pipeline_imports.py``).
    Format: ``YYYYMMDD-HHMMSS-<6hex>``.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _new_job_id() -> str:
    """Return a fresh opaque job id."""
    return uuid.uuid4().hex


def _row_to_job_detail(row: dict[str, object]) -> JobDetail:
    """Convert a ``web_jobs`` row (dict) into a :class:`JobDetail`."""
    return JobDetail(
        job_id=str(row["job_id"]),
        submitted_at=str(row["submitted_at"]),
        submitted_by=str(row["submitted_by"]),
        parent_job_id=(str(row["parent_job_id"]) if row.get("parent_job_id") else None),
        pipeline=str(row["pipeline"]),
        batch_id=str(row["batch_id"]),
        input_address=str(row["input_address"]),
        output_address=str(row["output_address"]),
        tables=json.loads(str(row["tables_json"] or "[]")),
        transformations=json.loads(str(row["transformations_json"] or "[]")),
        extra_args=json.loads(str(row["extra_args_json"] or "{}")),
        status=str(row["status"]),
        cancel_requested=bool(row["cancel_requested"]),
        instance_id=(str(row["instance_id"]) if row.get("instance_id") else None),
        pid=(int(row["pid"]) if row.get("pid") else None),
        started_at=(str(row["started_at"]) if row.get("started_at") else None),
        ended_at=(str(row["ended_at"]) if row.get("ended_at") else None),
        error=(str(row["error"]) if row.get("error") else None),
    )


def _fetchrow(conn, job_id: str) -> dict[str, object] | None:
    """Return one ``web_jobs`` row as a plain dict."""
    row = conn.execute("SELECT * FROM web_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Submit / rerun
# ---------------------------------------------------------------------------


def submit(request: JobSubmitRequest, *, actor: str = "web") -> JobSubmitResponse:
    """Validate and enqueue a new ETL job.

    Args:
        request: Parsed submit-form payload.
        actor: Free-form origin label (``web`` / ``mcp:<client>``).

    Returns:
        A :class:`JobSubmitResponse` carrying the new ``job_id`` and
        the pre-assigned ``instance_id``.

    Raises:
        SubmitError: When a required input references something that
            does not exist (pipeline, address config, empty batch_id, …).
    """
    return _insert_job(request, actor=actor, status=STATUS_QUEUED)


def create_draft(request: JobSubmitRequest, *, actor: str = "web") -> JobSubmitResponse:
    """Validate and store a job as a ``draft`` — fully specified, not queued.

    A draft runs exactly the same submit-time validation as
    :func:`submit` and is persisted with a pre-assigned ``instance_id``
    so its run page (``/runs/<instance_id>``) renders every run detail.
    The worker only claims ``queued`` rows, so a draft never executes
    until a human launches it with :func:`launch_draft` (the ▶ button on
    the run page). This is how the assistant hands a ready-to-run job to
    the operator without ever executing it.

    Raises:
        SubmitError: Same validation failures as :func:`submit`.
    """
    return _insert_job(request, actor=actor, status=STATUS_DRAFT)


def launch_draft(job_id: str, *, actor: str = "web") -> JobDetail:
    """Promote a ``draft`` job to ``queued`` so the worker picks it up.

    The transition is a single guarded ``UPDATE … WHERE status='draft'``
    so a double-click cannot launch the same draft twice.

    Raises:
        SubmitError: When the job does not exist or is not a draft.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
        if row is None:
            raise SubmitError(f"Job '{job_id}' not found.")
        cur = conn.execute(
            "UPDATE web_jobs SET status = ?, submitted_at = ? "
            "WHERE job_id = ? AND status = ?",
            (STATUS_QUEUED, _now_iso(), job_id, STATUS_DRAFT),
        )
        if cur.rowcount != 1:
            raise SubmitError(
                f"Job '{job_id}' is {row['status']!s}, not a draft; nothing to launch."
            )
        row = _fetchrow(conn, job_id)
    assert row is not None
    web_db.audit(
        actor=actor,
        action="run.launch",
        target=f"{row['pipeline']}:{row['batch_id']}",
        detail_json=json.dumps({"job_id": job_id, "instance_id": row.get("instance_id")}),
    )
    logger.info("Launched draft %s (pipeline=%s batch=%s)", job_id,
                row["pipeline"], row["batch_id"])
    return _row_to_job_detail(row)


def discard_draft(job_id: str, *, actor: str = "web") -> None:
    """Delete a ``draft`` job. Only drafts can be discarded.

    Raises:
        SubmitError: When the job does not exist or is not a draft.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
        if row is None:
            raise SubmitError(f"Job '{job_id}' not found.")
        cur = conn.execute(
            "DELETE FROM web_jobs WHERE job_id = ? AND status = ?",
            (job_id, STATUS_DRAFT),
        )
        if cur.rowcount != 1:
            raise SubmitError(
                f"Job '{job_id}' is {row['status']!s}, not a draft; it cannot be discarded."
            )
    web_db.audit(
        actor=actor,
        action="run.discard_draft",
        target=f"{row['pipeline']}:{row['batch_id']}",
        detail_json=json.dumps({"job_id": job_id}),
    )


def _insert_job(
    request: JobSubmitRequest, *, actor: str, status: str
) -> JobSubmitResponse:
    """Shared validate-and-insert path for :func:`submit` / :func:`create_draft`."""
    _resolve_connection_endpoints(request, actor=actor)
    _validate_submit(request)
    web_db.ensure_schema()
    job_id = _new_job_id()
    instance_id = _new_instance_id()
    submitted_at = _now_iso()

    kind = normalize_kind(request.kind)
    extra_args = {
        "kind": kind,
        "cdm": request.cdm,
        "engine": request.engine,
        "workers": request.workers,
        "sf_database": request.sf_database,
        "sf_account": request.sf_account,
        "sf_schema": request.sf_schema,
        "stderr_log": bool(request.stderr_log),
    }
    # ``extra_mounts`` is accepted only on kinds whose spec allows it
    # (copy / move). Persist verbatim so the worker's argv builder can
    # forward each entry through ``--extra-mount``. The CLI re-validates
    # on host-side dispatch — see :mod:`ovid2.tools.extra_mounts`.
    spec = get_kind(kind)
    if spec and spec.allows_extra_mounts and request.extra_mounts:
        extra_args["extra_mounts"] = list(request.extra_mounts)
    # ``strict_types`` is accepted only on kinds whose spec allows it
    # (move). Persist verbatim so the worker's ``_build_argv_move`` can
    # emit ``--strict-types`` when the operator opted in. Rejected
    # server-side on every other kind (see ``_validate_submit`` below).
    if spec and spec.allows_strict_types and request.strict_types:
        extra_args["strict_types"] = True

    # ``parts`` / ``packages`` / ``checks`` / ``prev_input_address`` are
    # health-shaped selection fields (``spec.health_selection``). Both
    # ``health`` and ``dq_checks`` run the check engine, so persist them
    # verbatim so the worker's ``_build_argv_health`` can forward them.
    # Ignored on every other kind.
    if spec and spec.health_selection:
        if request.parts and request.parts != ["all"]:
            extra_args["parts"] = list(request.parts)
        if request.packages:
            extra_args["packages"] = list(request.packages)
        if request.checks:
            extra_args["checks"] = list(request.checks)
        if request.prev_input_address:
            extra_args["prev_input_address"] = request.prev_input_address

    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs "
            "(job_id, submitted_at, submitted_by, parent_job_id, pipeline, batch_id, "
            "input_address, output_address, tables_json, transformations_json, "
            "extra_args_json, status, cancel_requested, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                job_id,
                submitted_at,
                actor,
                None,
                request.pipeline,
                request.batch_id,
                request.input_address,
                request.output_address,
                json.dumps(request.tables),
                json.dumps(request.transformations),
                json.dumps(extra_args),
                status,
                instance_id,
            ),
        )

    web_db.audit(
        actor=actor,
        action="run.draft" if status == STATUS_DRAFT else "run.submit",
        target=f"{request.pipeline}:{request.batch_id}",
        detail_json=json.dumps(
            {"job_id": job_id, "instance_id": instance_id, "extra_args": extra_args}
        ),
    )
    logger.info(
        "%s job %s (pipeline=%s batch_id=%s instance=%s)",
        "Drafted" if status == STATUS_DRAFT else "Submitted",
        job_id,
        request.pipeline,
        request.batch_id,
        instance_id,
    )
    return JobSubmitResponse(job_id=job_id, instance_id=instance_id, status=status)


def _resolve_connection_endpoints(request: JobSubmitRequest, *, actor: str) -> None:
    """Turn run-time storage selections into resolved address names.

    Mirrors the /submit 'Browse storage' picker for the JSON API + MCP:
    when a caller supplies ``input_connection`` / ``output_connection``
    (with an optional ``*_location``) instead of a saved address, resolve
    -or-create an address from ``{connection, location}`` and write the
    result back onto ``request.input_address`` / ``request.output_address``.
    An explicit saved address always wins if both are given.

    This is what lets a reusable EDC / health run target any database:
    the operator (or Agent Ovid) selects a storage account and a
    database/schema at submit time; no per-partner pipeline package or
    pre-saved address is required.

    Raises:
        SubmitError: When the resolve-or-create fails (e.g. unknown
            connection).
    """
    from ovid2.web.services import addresses_svc

    def _resolve(address: str, connection: str, location: str) -> str:
        if address.strip():
            return address
        if not connection.strip():
            return address
        folder = (location or "").strip().strip("/")
        if folder[:1] in ("{", "["):
            raise SubmitError(
                "location must be a plain path like DATABASE/SCHEMA, not JSON; "
                "prefer a saved address name (input_address / output_address)."
            )
        loc: dict[str, object] = {"path": folder} if folder else {}
        try:
            return addresses_svc.resolve_or_create_address(
                connection.strip(), loc, actor=actor
            )
        except addresses_svc.AddressError as exc:
            raise SubmitError(str(exc)) from exc

    request.input_address = _resolve(
        request.input_address, request.input_connection, request.input_location
    )
    request.output_address = _resolve(
        request.output_address, request.output_connection, request.output_location
    )


def _validate_submit(request: JobSubmitRequest) -> None:
    """Best-effort pre-flight checks. Raises :class:`SubmitError` on failure."""
    if not request.pipeline.strip():
        raise SubmitError("pipeline is required.")
    if not request.batch_id.strip():
        raise SubmitError("batch_id is required.")
    kind = normalize_kind(request.kind)
    # Package-free kinds (health / dq_checks / copy / move) run without an
    # ``ovid2_pipelines.<name>`` package — ``pipeline`` is only a
    # name/label for address path scoping — so we must not require an
    # installed package for them. Package-consuming kinds (etl /
    # analysis) keep the gate.
    if (
        kind not in PACKAGE_FREE_KINDS
        and request.pipeline not in set(pipelines_meta_svc.discover_pipelines())
    ):
        raise SubmitError(
            f"Pipeline '{request.pipeline}' is not installed under ovid2_pipelines."
        )
    known = set(_addresses.list_available_addresses())
    if known:
        # Only enforce when *some* configs exist — a fresh install has
        # none, and the doctor page already surfaces that.
        for label, value in (
            ("input_address", request.input_address),
            ("output_address", request.output_address),
        ):
            if value not in known:
                raise SubmitError(
                    f"{label} '{value}' is not on the config search path."
                )
    if request.workers < 1 or request.workers > 200:
        raise SubmitError("workers must be between 1 and 200.")

    # Kind-specific checks, driven by the run-kind registry.
    spec = get_kind(kind)
    if spec and spec.tables_mode == "explicit" and (
        not request.tables or request.tables == ["all"]
    ):
        # ``ovid2 tools copy --tables`` / ``ovid2 tools move --tables``
        # both use ``nargs='+'`` and do not recognise the ``all``
        # sentinel *by default*. A source whose driver type can expand
        # ``all`` (e.g. Access — a self-describing container of tables)
        # is exempt: the executor enumerates the tables at run time.
        from ovid2.web.services._table_expand import source_supports_expand_all

        if not (
            request.tables == ["all"]
            and source_supports_expand_all(request.input_address)
        ):
            raise SubmitError(
                f"{kind} jobs require an explicit `tables` list; "
                f"`['all']` is not supported by `ovid2 {spec.cli_subcommand}` "
                f"for this source."
            )
    if (
        spec
        and not spec.uses_transformations
        and spec.tables_mode == "explicit"
        and request.transformations
        and request.transformations != ["all"]
    ):
        raise SubmitError(
            f"{kind} jobs do not accept `transformations` (formatters, "
            "mappers, and fixers are bypassed by design)."
        )
    if spec and spec.allows_extra_mounts:
        # Validate every extra-mount spec now so the operator sees a
        # clean rejection at submit time instead of a worker failure
        # 30 seconds into the container start. ``parse_extra_mounts``
        # verifies absolute paths, host existence, mode, and the
        # reserved-container-path list. The parsed results are
        # discarded here; the worker forwards raw strings to the CLI
        # which re-parses inside its own error boundary.
        from ovid2.tools.extra_mounts import ExtraMountError, parse_extra_mounts

        try:
            parse_extra_mounts(request.extra_mounts)
        except ExtraMountError as exc:
            raise SubmitError(f"extra_mounts: {exc}") from None
    else:
        if request.extra_mounts:
            raise SubmitError(
                f"extra_mounts is only accepted on kind='copy' or "
                f"kind='move'; got kind='{kind}' with "
                f"{len(request.extra_mounts)} mount(s)."
            )
    # ``strict_types`` is accepted only on kinds whose spec allows it
    # (move). Refuse the flag elsewhere so a mis-authored payload can't
    # quietly enable it.
    if (not spec or not spec.allows_strict_types) and request.strict_types:
        raise SubmitError(
            f"strict_types is only accepted on kind='move'; got "
            f"kind='{kind}'."
        )

    # Engine/driver family compatibility. Refuses submissions that
    # would have crashed inside the container at ``engine.get_session()``
    # (e.g. ``--engine snowpark`` with a ``disc``-typed input, which
    # used to spin up a 25-worker Spark cluster before failing at
    # ``from snowflake.snowpark import Session`` or later at
    # ``s3a://…`` write time). The same check runs again host-side in
    # ``ovid2 etl`` / ``ovid2 tools copy`` / ``ovid2 tools move`` and
    # again container-side; the web pre-flight is the earliest surface
    # — a bad pairing rejects ``POST /api/v1/runs`` with HTTP 400
    # before the row is ever inserted into ``web_jobs``. Unresolvable
    # addresses (unregistered type, missing config file) and unknown
    # engine type names are skipped inside
    # ``assert_engine_type_supports_drivers`` — the run proceeds, and
    # any downstream failure gets to speak for itself. Uses the
    # type-name variant of the check so this module stays free of
    # ``ovid2.core.*`` imports (see
    # ``tests/contract/test_web_no_pipeline_imports.py``).
    engine_name = (request.engine or "").strip() or "pyspark"
    try:
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("engine_compat import failed; skipping check: %s", exc)
        return
    try:
        assert_engine_type_supports_drivers(
            engine_name, [request.input_address, request.output_address]
        )
    except RuntimeError as exc:
        raise SubmitError(str(exc)) from None


def rerun(job_id: str, *, actor: str = "web") -> JobSubmitResponse:
    """Enqueue a byte-identical retry of an existing job.

    Args:
        job_id: ID of the source job.
        actor: Origin label recorded on the new row and audit entry.

    Returns:
        A :class:`JobSubmitResponse` for the *new* job.

    Raises:
        SubmitError: When the source job does not exist or is currently
            queued/running (nothing to rerun yet).
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
    if row is None:
        raise SubmitError(f"Job '{job_id}' not found.")
    if row["status"] in {STATUS_QUEUED, STATUS_RUNNING, STATUS_DRAFT}:
        raise SubmitError(
            f"Job '{job_id}' is {row['status']!s}; wait for it to finish or cancel it first."
        )

    new_job_id = _new_job_id()
    instance_id = _new_instance_id()
    submitted_at = _now_iso()

    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs "
            "(job_id, submitted_at, submitted_by, parent_job_id, pipeline, batch_id, "
            "input_address, output_address, tables_json, transformations_json, "
            "extra_args_json, status, cancel_requested, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                new_job_id,
                submitted_at,
                actor,
                job_id,
                row["pipeline"],
                row["batch_id"],
                row["input_address"],
                row["output_address"],
                row["tables_json"],
                row["transformations_json"],
                row["extra_args_json"],
                STATUS_QUEUED,
                instance_id,
            ),
        )

    # Reset the scheduler tracker row so the batch is treated as fresh.
    _reset_tracker(str(row["batch_id"]))

    web_db.audit(
        actor=actor,
        action="run.rerun",
        target=f"{row['pipeline']}:{row['batch_id']}",
        detail_json=json.dumps({"parent_job_id": job_id, "new_job_id": new_job_id}),
    )
    logger.info("Rerun %s -> %s (pipeline=%s batch=%s)", job_id, new_job_id,
                row["pipeline"], row["batch_id"])
    return JobSubmitResponse(
        job_id=new_job_id, instance_id=instance_id, status=STATUS_QUEUED
    )


def _reset_tracker(batch_id: str) -> None:
    """Reset the scheduler tracker row for a batch back to ``NEW``.

    Silently no-ops when the tracker database file is missing (fresh
    install) or when the batch id is not registered (web-only submit).
    """
    path = _default_sqlite_path()
    if not path.is_file():
        return
    try:
        tracker = SqliteBatchTracker(path=path)
        tracker.initialize_tables()
        current = tracker.get_batch_status(batch_id)
        if current is None:
            return
        tracker.reset_to_new(batch_id)
    except Exception:  # pragma: no cover — defensive
        logger.exception("Failed to reset tracker row for batch %s", batch_id)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def cancel(job_id: str, *, actor: str = "web") -> JobDetail:
    """Request cancellation of a job.

    ``queued`` jobs transition straight to ``cancelled``; ``running``
    jobs get their ``cancel_requested`` flag set and the worker will
    send SIGTERM on its next poll cycle.

    Raises:
        SubmitError: When the job does not exist or is already terminal.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
        if row is None:
            raise SubmitError(f"Job '{job_id}' not found.")
        status = str(row["status"])
        if status in {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}:
            raise SubmitError(f"Job '{job_id}' is already {status}.")
        if status == STATUS_DRAFT:
            raise SubmitError(
                f"Job '{job_id}' is a draft; discard it instead of cancelling."
            )
        if status == STATUS_QUEUED:
            conn.execute(
                "UPDATE web_jobs SET status = ?, cancel_requested = 1, ended_at = ? "
                "WHERE job_id = ?",
                (STATUS_CANCELLED, _now_iso(), job_id),
            )
        else:
            conn.execute(
                "UPDATE web_jobs SET cancel_requested = 1 WHERE job_id = ?",
                (job_id,),
            )
        row = _fetchrow(conn, job_id)

    web_db.audit(
        actor=actor,
        action="run.cancel",
        target=job_id,
        detail_json=json.dumps({"prev_status": status}),
    )
    assert row is not None
    return _row_to_job_detail(row)


# ---------------------------------------------------------------------------
# Worker helpers — public so tests + the worker can reuse them.
# ---------------------------------------------------------------------------


def claim_next_queued() -> JobDetail | None:
    """Atomically claim the oldest queued job and mark it ``running``.

    Returns:
        The claimed :class:`JobDetail`, or ``None`` when no queued job
        is available.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        # ``BEGIN IMMEDIATE`` prevents another poller from claiming the
        # same row between the SELECT and UPDATE.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM web_jobs WHERE status = ? ORDER BY submitted_at ASC LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        job_id = str(row["job_id"])
        conn.execute(
            "UPDATE web_jobs SET status = ?, started_at = ? WHERE job_id = ?",
            (STATUS_RUNNING, _now_iso(), job_id),
        )
        conn.execute("COMMIT")
        row = _fetchrow(conn, job_id)
    assert row is not None
    return _row_to_job_detail(row)


def mark_running_pid(job_id: str, pid: int) -> None:
    """Record the subprocess PID once the worker has spawned ``ovid2 etl``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute("UPDATE web_jobs SET pid = ? WHERE job_id = ?", (int(pid), job_id))


def mark_terminal(job_id: str, *, status: str, error: str | None = None) -> None:
    """Transition a job into a terminal state (``done``/``failed``/``cancelled``)."""
    if status not in {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}:
        raise ValueError(f"Invalid terminal status: {status!r}")
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_jobs SET status = ?, ended_at = ?, error = ? WHERE job_id = ?",
            (status, _now_iso(), error, job_id),
        )


def resume_in_flight() -> int:
    """Fail every ``running`` row whose PID is not alive.

    Called by the worker at start-up. Returns the number of rows
    reset. Rows with ``cancel_requested = 1`` are marked ``cancelled``;
    the rest are marked ``failed`` with a clear error message so the
    operator knows the crash was recovered, not silently ignored.
    """
    web_db.ensure_schema()
    reset = 0
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT job_id, pid, cancel_requested FROM web_jobs WHERE status = ?",
            (STATUS_RUNNING,),
        ).fetchall()
        for row in rows:
            pid = row["pid"]
            if pid is not None and _pid_is_alive(int(pid)):
                continue
            new_status = STATUS_CANCELLED if row["cancel_requested"] else STATUS_FAILED
            conn.execute(
                "UPDATE web_jobs SET status = ?, ended_at = ?, error = ? "
                "WHERE job_id = ?",
                (
                    new_status,
                    _now_iso(),
                    None if new_status == STATUS_CANCELLED else "worker crash recovered",
                    row["job_id"],
                ),
            )
            reset += 1
    if reset:
        logger.warning("Resumed %d in-flight web_jobs row(s) after worker restart", reset)
    return reset


def _pid_is_alive(pid: int) -> bool:
    """Return True when ``pid`` names a running process owned by us."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
    return True


# ---------------------------------------------------------------------------
# Query helpers used by routes
# ---------------------------------------------------------------------------


def get_job(job_id: str) -> JobDetail | None:
    """Return one job by id, or ``None`` when unknown."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
    return None if row is None else _row_to_job_detail(row)


def get_job_by_instance(instance_id: str) -> JobDetail | None:
    """Return the job that owns a given ``instance_id``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_jobs WHERE instance_id = ? LIMIT 1", (instance_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_job_detail(dict(row))


def queue_position(job_id: str) -> dict[str, int] | None:
    """Describe what a ``queued`` job is waiting behind.

    The worker claims ``queued`` rows oldest-``submitted_at`` first and
    runs at most ``WEB_MAX_PARALLEL_JOBS`` at a time, so a freshly
    launched job can sit queued for the whole duration of an earlier
    run. The UI uses this to say *why* nothing is happening yet instead
    of looking identical to a draft.

    Args:
        job_id: The job to locate in the queue.

    Returns:
        ``{"running": <n running jobs>, "ahead": <n queued jobs claimed
        before this one>}`` when the job is queued, else ``None``.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
        if row is None or row["status"] != STATUS_QUEUED:
            return None
        running = conn.execute(
            "SELECT COUNT(*) FROM web_jobs WHERE status = ?", (STATUS_RUNNING,)
        ).fetchone()[0]
        ahead = conn.execute(
            "SELECT COUNT(*) FROM web_jobs WHERE status = ? AND job_id != ? "
            "AND submitted_at < ?",
            (STATUS_QUEUED, job_id, row["submitted_at"]),
        ).fetchone()[0]
    return {"running": int(running), "ahead": int(ahead)}


def statuses_for_instances(instance_ids: list[str]) -> dict[str, str]:
    """Return ``{instance_id: web_jobs.status}`` for the given instances.

    Backs the lightweight run-list badge refresh so the Jobs list stops
    showing a stale ``DRAFT`` badge after ▶ launch. Unknown ids are
    omitted.
    """
    ids = [i for i in dict.fromkeys(instance_ids) if i][:200]
    if not ids:
        return {}
    web_db.ensure_schema()
    placeholders = ",".join("?" for _ in ids)
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT instance_id, status FROM web_jobs WHERE instance_id IN ("
            + placeholders + ")",
            tuple(ids),
        ).fetchall()
    return {str(r["instance_id"]): str(r["status"]) for r in rows}


def list_jobs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    pipeline: str | None = None,
) -> JobListResponse:
    """Return a paginated slice of the ``web_jobs`` table."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if status is not None and status not in _VALID_STATUSES:
        raise SubmitError(f"Invalid status filter '{status}'.")
    web_db.ensure_schema()
    with web_db.connect() as conn:
        if status is not None and pipeline is not None:
            total = conn.execute(
                "SELECT COUNT(*) FROM web_jobs WHERE status = ? AND pipeline = ?",
                (status, pipeline),
            ).fetchone()[0]
            cursor = conn.execute(
                "SELECT * FROM web_jobs WHERE status = ? AND pipeline = ? "
                "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                (status, pipeline, limit, offset),
            )
        elif status is not None:
            total = conn.execute(
                "SELECT COUNT(*) FROM web_jobs WHERE status = ?", (status,)
            ).fetchone()[0]
            cursor = conn.execute(
                "SELECT * FROM web_jobs WHERE status = ? "
                "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        elif pipeline is not None:
            total = conn.execute(
                "SELECT COUNT(*) FROM web_jobs WHERE pipeline = ?", (pipeline,)
            ).fetchone()[0]
            cursor = conn.execute(
                "SELECT * FROM web_jobs WHERE pipeline = ? "
                "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                (pipeline, limit, offset),
            )
        else:
            total = conn.execute("SELECT COUNT(*) FROM web_jobs").fetchone()[0]
            cursor = conn.execute(
                "SELECT * FROM web_jobs ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = [dict(r) for r in cursor.fetchall()]
    return JobListResponse(
        total=int(total),
        limit=limit,
        offset=offset,
        jobs=[_row_to_job_detail(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# Log path
# ---------------------------------------------------------------------------


def worker_log_path(job_id: str) -> Path:
    """Return the on-disk path for one job's captured stderr log."""
    log_dir = web_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{job_id}.log"
