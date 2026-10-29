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
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    OdsExportJobRequest,
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
    {STATUS_DRAFT, STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}
)


class SubmitError(ValueError):
    """Raised when a submit request fails validation."""


class OverwriteRequired(SubmitError):
    """Raised when the output location already holds tables and no overwrite was confirmed.

    The output address is the exact write location (see
    ``adr-address-is-exact-location.md``), so writing into an occupied
    output replaces existing data. HTTP surfaces map this to
    ``409 overwrite_required`` and re-submit with ``confirm_overwrite``.

    Attributes:
        output_address: The occupied output address.
        tables: The tables already present there.
    """

    def __init__(self, output_address: str, tables: list[str]) -> None:
        self.output_address = output_address
        self.tables = list(tables)
        preview = ", ".join(self.tables[:10]) + (" …" if len(self.tables) > 10 else "")
        super().__init__(
            f"Output '{output_address}' already contains {len(self.tables)} table(s) "
            f"({preview}). This run would overwrite them — confirm the overwrite "
            "to proceed."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _new_instance_id() -> str:
    """Return a fresh instance id matching :mod:`ovid2.core.isolation`.

    We deliberately re-implement the format here rather than importing
    ``ovid2.core.isolation`` — the web tier must not import from
    ``ovid2.core`` (see ``tests/contract/test_web_no_pipeline_imports.py``).
    Format: ``YYYYMMDD-HHMMSS-<6hex>``.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
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
        pid=(int(cast(int | str, row["pid"])) if row.get("pid") else None),
        started_at=(str(row["started_at"]) if row.get("started_at") else None),
        ended_at=(str(row["ended_at"]) if row.get("ended_at") else None),
        error=(str(row["error"]) if row.get("error") else None),
    )


def _fetchrow(conn: sqlite3.Connection, job_id: str) -> dict[str, object] | None:
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


def _row_overwrite_gate(row: dict[str, object], *, confirm_overwrite: bool) -> str:
    """Run the overwrite gate for a stored job row; return its (updated) extra_args JSON.

    Raises:
        OverwriteRequired: When the output is occupied and the overwrite
            was neither confirmed now nor at submit time.
    """
    extra = json.loads(str(row.get("extra_args_json") or "{}"))
    confirmed = bool(confirm_overwrite or extra.get("overwrite"))
    probe_req = JobSubmitRequest(
        kind=str(extra.get("kind") or "etl"),
        pipeline=str(row["pipeline"]),
        batch_id=str(row["batch_id"]),
        input_address=str(row["input_address"]),
        output_address=str(row["output_address"]),
        tables=json.loads(str(row.get("tables_json") or "[]")) or ["all"],
    )
    collisions = check_overwrite(str(row["output_address"]), probe_req, confirmed=confirmed)
    if collisions or confirm_overwrite:
        extra["overwrite"] = True
    return json.dumps(extra)


def launch_draft(job_id: str, *, actor: str = "web", confirm_overwrite: bool = False) -> JobDetail:
    """Promote a ``draft`` job to ``queued`` so the worker picks it up.

    The transition is a single guarded ``UPDATE … WHERE status='draft'``
    so a double-click cannot launch the same draft twice. The overwrite
    gate runs here (a draft was not checked when stored).

    Raises:
        SubmitError: When the job does not exist or is not a draft.
        OverwriteRequired: When the output is occupied and the
            overwrite was not confirmed.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = _fetchrow(conn, job_id)
    if row is None:
        raise SubmitError(f"Job '{job_id}' not found.")
    if row["status"] != STATUS_DRAFT:
        raise SubmitError(f"Job '{job_id}' is {row['status']!s}, not a draft; nothing to launch.")
    extra_json = _row_overwrite_gate(row, confirm_overwrite=confirm_overwrite)
    with web_db.connect() as conn:
        cur = conn.execute(
            "UPDATE web_jobs SET status = ?, submitted_at = ?, extra_args_json = ? "
            "WHERE job_id = ? AND status = ?",
            (STATUS_QUEUED, _now_iso(), extra_json, job_id, STATUS_DRAFT),
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
    logger.info(
        "Launched draft %s (pipeline=%s batch=%s)", job_id, row["pipeline"], row["batch_id"]
    )
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


def _insert_job(request: JobSubmitRequest, *, actor: str, status: str) -> JobSubmitResponse:
    """Shared validate-and-insert path for :func:`submit` / :func:`create_draft`."""
    _job_only = get_kind(request.kind)
    if _job_only is not None and _job_only.job_only:
        raise SubmitError(
            f"kind='{_job_only.name}' jobs are created from their own surface "
            "(POST /api/v1/dataspace/table/export-jobs), not POST /runs."
        )
    _fold_legacy_sf_location(request)
    _resolve_connection_endpoints(request, actor=actor)
    _derive_batch_id(request)
    _expand_all_tables(request)
    _validate_submit(request)
    # A draft is not executed until a human launches it; the overwrite
    # check runs (again) at launch time against the then-current output.
    if status != STATUS_DRAFT:
        check_overwrite(request.output_address, request, confirmed=request.confirm_overwrite)
    web_db.ensure_schema()
    job_id = _new_job_id()
    instance_id = _new_instance_id()
    submitted_at = _now_iso()

    kind = normalize_kind(request.kind)
    extra_args: dict[str, Any] = {
        "kind": kind,
        "cdm": request.cdm,
        "engine": request.engine,
        "workers": request.workers,
        "sf_account": request.sf_account,
        "stderr_log": bool(request.stderr_log),
    }
    # Operator-confirmed overwrite of an occupied output location; the
    # worker forwards it as ``--overwrite`` so the executor gate allows it.
    if request.confirm_overwrite:
        extra_args["overwrite"] = True
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


def export_artifact_path(job_id: str, fmt: str) -> Path:
    """Return where an ``export`` job writes its file (mode-0700 folder)."""
    from ovid2.web.services import dataspace_table_svc

    folder = dataspace_table_svc.export_dir() / f"job-{job_id}"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / f"export.{dataspace_table_svc.EXPORT_FORMATS[fmt][0]}"


def submit_export(request: OdsExportJobRequest, *, actor: str = "web") -> JobSubmitResponse:
    """Queue a full-table export of one Data Space table (``export`` kind).

    The worker runs ``ovid2 tools export-table`` on the host (no
    container); the finished file is served by
    ``GET /api/v1/jobs/{job_id}/artifact``. Only ``csv`` / ``xlsx`` are
    accepted — a full table as a PDF / Word document is not useful.

    Raises:
        SubmitError: Bad path / format / delimiter.
    """
    from ovid2 import dataspace
    from ovid2.dataspace.path import OdsPathError
    from ovid2.dataspace.resolver import OdsResolutionError

    if request.format not in ("csv", "xlsx"):
        raise SubmitError("Background exports support csv and xlsx only.")
    if request.header_case not in ("asis", "upper", "lower"):
        raise SubmitError("header_case must be asis / upper / lower.")
    delimiter = request.delimiter or ","
    if delimiter.lower() in ("tab", "\\t", "\t"):
        delimiter = "tab"
    elif len(delimiter) != 1 or delimiter in ('"', "\r", "\n"):
        raise SubmitError("The CSV delimiter must be a single character (or 'tab').")
    try:
        target = dataspace.resolve(request.path)
    except (OdsPathError, OdsResolutionError) as exc:
        raise SubmitError(str(exc)) from exc
    if target.connection is None or not target.location:
        raise SubmitError("A table path is /<account>/<folder…>/<table>.")

    web_db.ensure_schema()
    job_id = _new_job_id()
    instance_id = _new_instance_id()
    extra_args: dict[str, Any] = {
        "kind": "export",
        "connection": target.connection,
        "location": target.location,
        "format": request.format,
        "delimiter": delimiter,
        "header_case": request.header_case,
        "artifact": str(export_artifact_path(job_id, request.format)),
    }
    table_name = target.location.rsplit("/", 1)[-1]
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs "
            "(job_id, submitted_at, submitted_by, parent_job_id, pipeline, batch_id, "
            "input_address, output_address, tables_json, transformations_json, "
            "extra_args_json, status, cancel_requested, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                job_id,
                _now_iso(),
                actor,
                None,
                "export",
                target.batch_id,
                target.path.as_str(),
                f"download:{request.format}",
                json.dumps([table_name]),
                json.dumps([]),
                json.dumps(extra_args),
                STATUS_QUEUED,
                instance_id,
            ),
        )
    web_db.audit(
        actor=actor,
        action="dataspace.export_job",
        target=target.path.as_str(),
        detail_json=json.dumps({"job_id": job_id, "format": request.format}),
    )
    return JobSubmitResponse(job_id=job_id, instance_id=instance_id, status=STATUS_QUEUED)


def export_artifact(job_id: str) -> tuple[Path, str] | None:
    """Return ``(file, download_name)`` for a finished ``export`` job, else ``None``."""
    job = get_job(job_id)
    if job is None or normalize_kind(job.extra_args.get("kind")) != "export":
        return None
    if job.status != STATUS_DONE:
        return None
    path = Path(str(job.extra_args.get("artifact") or ""))
    if not path.is_file():
        return None
    from ovid2.drivers._table_listing import strip_table_extension

    leaf = str(job.extra_args.get("location") or "export").rsplit("/", 1)[-1]
    stem = "".join(c if (c.isalnum() or c in "._-") else "_" for c in strip_table_extension(leaf))
    return path, f"{stem or 'export'}{path.suffix}"


def _fold_legacy_sf_location(request: JobSubmitRequest) -> None:
    """Fold deprecated ``sf_database`` / ``sf_schema`` into the run-time location.

    The address is the exact location; a schema is no longer an
    override layered on top of it. A legacy caller that still sends
    ``sf_database`` / ``sf_schema`` together with an ``input_connection``
    (or ``output_connection``) has them folded into that side's
    ``DATABASE/SCHEMA`` location. Otherwise they are rejected so the
    caller learns to put the schema on the address.

    Raises:
        SubmitError: When the overrides cannot be folded into a location.
    """
    db = (request.sf_database or "").strip()
    schema = (request.sf_schema or "").strip()
    request.sf_database = None
    request.sf_schema = None
    if not db and not schema:
        return
    folded = False
    for side in ("input", "output"):
        if getattr(request, f"{side}_address").strip():
            continue
        if not getattr(request, f"{side}_connection").strip():
            continue
        parts = [p for p in getattr(request, f"{side}_location").strip("/").split("/") if p]
        cur_db = parts[0] if parts else ""
        cur_schema = parts[1] if len(parts) > 1 else ""
        new = "/".join(p for p in ((db or cur_db), (schema or cur_schema)) if p)
        setattr(request, f"{side}_location", new)
        folded = True
    if not folded:
        raise SubmitError(
            "sf_database / sf_schema are no longer overrides: every address is the "
            "exact location of its data. Pick the DATABASE/SCHEMA as the address "
            "(input_connection + input_location, or a saved address)."
        )


def _derive_batch_id(request: JobSubmitRequest) -> None:
    """Set ``request.batch_id`` to the complete Data Space path of the input.

    The batch id is ``<connection>/<location path>`` of the input
    address (``inbox/landing/2026_03``, ``sf_prod/CHAR_DB/SITE_X``). It
    is a label only — no driver appends it to an address. Legacy
    (connectionless) inputs keep the caller-supplied label.
    """
    from ovid2.web.services import output_probe_svc

    derived = output_probe_svc.batch_id_for_address(request.input_address)
    if derived:
        request.batch_id = derived


def check_overwrite(
    output_address: str,
    request: JobSubmitRequest | None = None,
    *,
    confirmed: bool,
) -> list[str]:
    """Raise :class:`OverwriteRequired` when the output is occupied and not confirmed.

    ``copy`` / ``move`` only count tables they would write; every other
    kind counts any table at the output (an ETL run replaces the whole
    CDM table set). ``health`` / ``dq_checks`` only write ``dcq_results``.
    Occupancy that cannot be determined never blocks — the executor's own
    ``--overwrite`` gate is the backstop.

    Returns:
        The colliding tables (``[]`` when none / unknown).
    """
    from ovid2.web.services import output_probe_svc

    probe = output_probe_svc.existing_output_tables(output_address)
    if not probe.occupied:
        return []
    tables = list(probe.tables)
    kind = normalize_kind(request.kind) if request is not None else "etl"
    spec = get_kind(kind)
    if request is not None and spec is not None and spec.health_selection:
        tables = [t for t in tables if t.lower() == "dcq_results"]
    elif (
        request is not None
        and spec is not None
        and spec.tables_mode == "explicit"
        and request.tables
        and request.tables != ["all"]
    ):
        wanted = {str(t).split("=", 1)[0].lower() for t in request.tables}
        tables = [t for t in tables if t.lower() in wanted]
    if tables and not confirmed:
        raise OverwriteRequired(output_address, tables)
    return tables


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
            return addresses_svc.resolve_or_create_address(connection.strip(), loc, actor=actor)
        except addresses_svc.AddressError as exc:
            raise SubmitError(str(exc)) from exc

    request.input_address = _resolve(
        request.input_address, request.input_connection, request.input_location
    )
    request.output_address = _resolve(
        request.output_address, request.output_connection, request.output_location
    )


def _expand_all_tables(request: JobSubmitRequest) -> None:
    """Replace a copy/move ``tables=['all']`` with the source's concrete roster.

    Runs at submit / draft time so the stored job (and the run page a
    human reviews before pressing ▶) lists exactly what will be copied,
    largest table first. Enumeration is delegated to the
    ``ovid2 tools list-source-tables`` executor broker — the web process
    never resolves a credential.

    Outcomes:

    - ``ok`` → ``request.tables`` becomes the explicit list.
    - ``unavailable`` (the driver type can enumerate, but not from this
      host — e.g. Access jars only in the image) → ``['all']`` is kept
      and the copy executor expands it at run time.
    - ``unsupported`` / ``error`` → left as ``['all']`` so
      :func:`_validate_submit` rejects it with the broker's reason.

    Non-explicit kinds and explicit lists are untouched.

    Raises:
        SubmitError: When the source can be listed but the listing
            failed (missing batch folder, bad schema, empty source).
    """
    kind = normalize_kind(request.kind)
    spec = get_kind(kind)
    if not spec or spec.tables_mode != "explicit" or request.tables != ["all"]:
        return
    if not request.input_address.strip():
        return
    from ovid2.web.services._table_expand import expand_source_tables

    outcome = expand_source_tables(
        request.input_address,
        pipeline=request.pipeline,
        batch_id=request.batch_id,
    )
    if outcome.code == "ok":
        logger.info(
            "Expanded %s tables=['all'] for %s → %d table(s)",
            kind,
            request.input_address,
            len(outcome.tables),
        )
        request.tables = list(outcome.tables)
        return
    if outcome.code == "error":
        raise SubmitError(
            f"{kind} jobs require an explicit `tables` list; could not expand "
            f"`['all']` for source '{request.input_address}' "
            f"(pipeline={request.pipeline!r}, batch_id={request.batch_id!r}): "
            f"{outcome.message or 'listing failed'}. Check the address / batch, "
            f"or pass the table names explicitly."
        )
    # ``unavailable`` keeps ``['all']`` for run-time expansion;
    # ``unsupported`` keeps it so ``_validate_submit`` rejects it.


def _validate_submit(request: JobSubmitRequest) -> None:
    """Best-effort pre-flight checks. Raises :class:`SubmitError` on failure."""
    if not request.pipeline.strip():
        raise SubmitError("pipeline is required.")
    if not request.batch_id.strip():
        raise SubmitError(
            "batch_id could not be derived: choose an input folder / schema "
            "(the batch id is its complete Data Space path)."
        )
    kind = normalize_kind(request.kind)
    # Package-free kinds (health / dq_checks / copy / move) run without an
    # ``ovid2_pipelines.<name>`` package — ``pipeline`` is only a
    # name/label for address path scoping — so we must not require an
    # installed package for them. Package-consuming kinds (etl /
    # analysis) keep the gate.
    if kind not in PACKAGE_FREE_KINDS and request.pipeline not in set(
        pipelines_meta_svc.discover_pipelines()
    ):
        raise SubmitError(f"Pipeline '{request.pipeline}' is not installed under ovid2_pipelines.")
    known = set(_addresses.list_available_addresses())
    if known:
        # Only enforce when *some* configs exist — a fresh install has
        # none, and the doctor page already surfaces that.
        for label, value in (
            ("input_address", request.input_address),
            ("output_address", request.output_address),
        ):
            if value not in known:
                raise SubmitError(f"{label} '{value}' is not on the config search path.")
    if request.workers < 1 or request.workers > 200:
        raise SubmitError("workers must be between 1 and 200.")

    # Kind-specific checks, driven by the run-kind registry.
    spec = get_kind(kind)
    if (
        spec
        and spec.tables_mode == "explicit"
        and (not request.tables or request.tables == ["all"])
    ):
        # ``ovid2 tools copy --tables`` / ``ovid2 tools move --tables``
        # both use ``nargs='+'``. ``['all']`` reaching here means
        # :func:`_expand_all_tables` could not resolve a concrete roster.
        # It is still accepted when the source's driver type can expand
        # ``all`` (the executor enumerates at run time); otherwise reject
        # with guidance an LLM caller can act on.
        from ovid2.web.services._table_expand import source_supports_expand_all

        if not (request.tables == ["all"] and source_supports_expand_all(request.input_address)):
            raise SubmitError(
                f"{kind} jobs require an explicit `tables` list; "
                f"`['all']` is not supported by `ovid2 {spec.cli_subcommand}` "
                f"for this source (its storage type cannot list its own "
                f"tables). Pass the source table names explicitly."
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
        raise SubmitError(f"strict_types is only accepted on kind='move'; got kind='{kind}'.")

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


def rerun(job_id: str, *, actor: str = "web", confirm_overwrite: bool = False) -> JobSubmitResponse:
    """Enqueue a retry of an existing job (same input, output, tables).

    A rerun writes to the same exact output location as the original, so
    the overwrite gate runs again: the previous run's tables are there.

    Args:
        job_id: ID of the source job.
        actor: Origin label recorded on the new row and audit entry.
        confirm_overwrite: Confirm overwriting the occupied output.

    Returns:
        A :class:`JobSubmitResponse` for the *new* job.

    Raises:
        SubmitError: When the source job does not exist or is currently
            queued/running (nothing to rerun yet).
        OverwriteRequired: When the output is occupied and the
            overwrite was not confirmed.
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
    if '"kind": "export"' in str(row.get("extra_args_json") or ""):
        raise SubmitError("Export jobs are not rerun; start a new export from the Data Space page.")

    extra_json = _row_overwrite_gate(row, confirm_overwrite=confirm_overwrite)
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
                extra_json,
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
    logger.info(
        "Rerun %s -> %s (pipeline=%s batch=%s)",
        job_id,
        new_job_id,
        row["pipeline"],
        row["batch_id"],
    )
    return JobSubmitResponse(job_id=new_job_id, instance_id=instance_id, status=STATUS_QUEUED)


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
            raise SubmitError(f"Job '{job_id}' is a draft; discard it instead of cancelling.")
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
                "UPDATE web_jobs SET status = ?, ended_at = ?, error = ? WHERE job_id = ?",
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
            "SELECT COUNT(*) FROM web_jobs WHERE status = ? AND job_id != ? AND submitted_at < ?",
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
            "SELECT instance_id, status FROM web_jobs WHERE instance_id IN (" + placeholders + ")",
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
