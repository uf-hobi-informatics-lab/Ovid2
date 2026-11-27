"""Workflow runner — executes workflow runs on the web worker (adr-workflows.md).

A *workflow run* is one execution of a workflow definition (frozen into
``web_workflow_runs.definition_json`` when it starts, so later edits
never change a run in flight). Each step is an ordinary job submitted
through :mod:`jobs_svc`, so every guarantee of the job queue — scoped
secrets, crash recovery, cancellation, audit, the run page — applies.

The worker calls :func:`advance_all` on every tick. Advancing is
idempotent and DB-driven (a worker restart simply resumes):

1. **Running.** Each settled step decides its outgoing links: a link is
   *taken* when the step's outcome matches the link's ``on``
   (``success`` / ``failure`` / ``always`` / ``otherwise``) and its
   optional ``when`` condition holds over the step's *facts* (run status,
   timeline, health summary, output tables, analysis row counts, engine
   metrics and data-probe numbers — never row values; see
   :mod:`ovid2.config.workflow_conditions`). A ``waiting`` step whose
   upstream steps have all settled either starts or is ``skipped``: with
   ``join: all`` every incoming link must be taken, with ``join: any`` at
   least one. Skipping propagates (a skipped step takes none of its
   links). A step that declares data probes is ``probing`` after its job
   succeeds: a ``workflow_probe`` job computes the numbers first. A
   failed step with a taken ``failure`` / ``always`` link is *handled*:
   its own output is rolled back and the failure path runs instead of
   aborting the workflow. A step that is started Its input is the upstream output for a ``data`` edge, the
   run's root input (watch batch / manual override) for a root step, or
   the step / pipeline input otherwise. The output is probed first:

   - occupied and the step may not overwrite → the step fails;
   - occupied and rollback is on → a ``workflow_storage`` *snapshot*
     job backs the tables up, then the step's job is queued;
   - otherwise the step's job is queued directly.

2. **Stopping.** When a step fails with no failure path (or the run is
   cancelled) every other active job is cancelled and waiting steps are
   ``skipped``.

3. **Rolling back.** Once nothing is active, every step that ran (or
   snapshotted) is undone in reverse topological order, one at a time:
   tables the run created are dropped, tables it overwrote are restored
   from the snapshot, then the snapshot is removed. A rollback job that
   fails leaves the run ``rollback_failed`` with its backups kept; the
   operator can retry with :func:`retry_rollback`.

4. **Finishing.** When every step is ``done``, ``skipped`` (a branch not
   taken) or a handled failure, the snapshots are removed and the run
   ``succeeded``.

Web-tier module: never imports ``ovid2.core``; storage operations run in
the ``ovid2 tools workflow-storage`` executor subprocess.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ovid2.config import workflow_conditions as wc
from ovid2.web import db as web_db
from ovid2.web.models import (
    WorkflowDetail,
    WorkflowEdge,
    WorkflowEdgeDecision,
    WorkflowRunDetail,
    WorkflowRunListResponse,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowWrite,
)
from ovid2.web.services import jobs_svc, pipelines_svc, workflows_svc
from ovid2.workspaces import DEFAULT_WORKSPACE, active_workspace, use_workspace

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "WorkflowRunError",
    "active_run",
    "advance_all",
    "advance_run",
    "cancel_run",
    "get_run",
    "list_runs",
    "retry_rollback",
    "start_run",
]

# Run statuses.
RUN_RUNNING = "running"
RUN_STOPPING = "stopping"
RUN_ROLLING_BACK = "rolling_back"
RUN_FINISHING = "finishing"
RUN_SUCCEEDED = "succeeded"
RUN_ROLLED_BACK = "rolled_back"
RUN_ROLLBACK_FAILED = "rollback_failed"
RUN_CANCELLED = "cancelled"

ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    {RUN_RUNNING, RUN_STOPPING, RUN_ROLLING_BACK, RUN_FINISHING}
)
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {RUN_SUCCEEDED, RUN_ROLLED_BACK, RUN_ROLLBACK_FAILED, RUN_CANCELLED}
)

# Step statuses.
ST_WAITING = "waiting"
ST_STARTING = "starting"
ST_SNAPSHOTTING = "snapshotting"
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"
ST_SKIPPED = "skipped"
ST_ROLLING_BACK = "rolling_back"
ST_ROLLED_BACK = "rolled_back"
ST_ROLLBACK_FAILED = "rollback_failed"
ST_CLEANING = "cleaning"
ST_PROBING = "probing"

_STEP_ACTIVE = frozenset({ST_STARTING, ST_SNAPSHOTTING, ST_QUEUED, ST_RUNNING, ST_PROBING})


class WorkflowRunError(ValueError):
    """Raised when a workflow run cannot be started or changed."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _run_row(run_id: str) -> dict[str, Any] | None:
    """Return one run of the active workspace (another workspace's run → ``None``)."""
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_workflow_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, active_workspace()),
        ).fetchone()
    return dict(row) if row is not None else None


def _step_rows(run_id: str) -> dict[str, dict[str, Any]]:
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM web_workflow_steps WHERE run_id = ?", (run_id,)
        ).fetchall()
    return {str(r["step_id"]): dict(r) for r in rows}


_STEP_COLUMNS = frozenset(
    {
        "status",
        "job_id",
        "input_address",
        "output_address",
        "pre_existing_tables_json",
        "backup_address",
        "snapshot_job_id",
        "rollback_job_id",
        "rollback_action",
        "rollback_plan_json",
        "error",
        "started_at",
        "ended_at",
        "outcome",
        "reason",
        "facts_json",
        "probe_job_id",
        "decisions_json",
    }
)
_RUN_COLUMNS = frozenset(
    {"status", "started_at", "ended_at", "error", "batch_id", "cancel_requested"}
)


def _update_step(run_id: str, step_id: str, **fields: Any) -> None:
    cols = [c for c in fields if c in _STEP_COLUMNS]
    if not cols:
        return
    assignments = ", ".join(c + " = ?" for c in cols)
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_workflow_steps SET " + assignments + " WHERE run_id = ? AND step_id = ?",
            (*[fields[c] for c in cols], run_id, step_id),
        )


def _claim_step(run_id: str, step_id: str, expect: str, new: str) -> bool:
    """Atomically move a step ``expect`` → ``new``; ``False`` if someone else did.

    The web process (``start_run`` / ``cancel_run``) and the worker both
    advance runs; this guarded UPDATE makes sure exactly one of them starts
    a given step (otherwise two identical jobs would race on one output).
    """
    with web_db.connect() as conn:
        cur = conn.execute(
            "UPDATE web_workflow_steps SET status = ? WHERE run_id = ? AND step_id = ? "
            "AND status = ?",
            (new, run_id, step_id, expect),
        )
        return cur.rowcount == 1


def _update_run(run_id: str, **fields: Any) -> None:
    cols = [c for c in fields if c in _RUN_COLUMNS]
    if not cols:
        return
    assignments = ", ".join(c + " = ?" for c in cols)
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_workflow_runs SET " + assignments + " WHERE run_id = ?",
            (*[fields[c] for c in cols], run_id),
        )


def _definition(run: dict[str, Any]) -> WorkflowWrite:
    return WorkflowWrite.model_validate(json.loads(str(run["definition_json"])))


def _job_status(job_id: str | None) -> str | None:
    if not job_id:
        return None
    job = jobs_svc.get_job(job_id)
    return job.status if job is not None else None


def _job_error(job_id: str | None) -> str | None:
    if not job_id:
        return None
    job = jobs_svc.get_job(job_id)
    return job.error if job is not None else None


def _json_obj(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(str(raw))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _decisions(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return a step's stored outgoing-link decisions, or ``None`` while undecided."""
    return _json_obj(row.get("decisions_json"))


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def _to_detail(run: dict[str, Any], *, with_definition: bool = True) -> WorkflowRunDetail:
    steps = _step_rows(str(run["run_id"]))
    definition = _definition(run)
    order = {s.id: i for i, s in enumerate(definition.steps)}
    out_steps: list[WorkflowStepRun] = []
    edge_out: list[WorkflowEdgeDecision] = []
    handled = 0
    skipped = 0
    for sid, row in sorted(steps.items(), key=lambda kv: order.get(kv[0], 999)):
        job = jobs_svc.get_job(str(row["job_id"])) if row.get("job_id") else None
        pre = row.get("pre_existing_tables_json")
        decided = _decisions(row)
        if decided is not None:
            for target, d in (decided.get("edges") or {}).items():
                edge_out.append(
                    WorkflowEdgeDecision(
                        source=sid,
                        target=target,
                        taken=bool(d.get("taken")),
                        reason=str(d.get("reason") or ""),
                    )
                )
            if decided.get("handled"):
                handled += 1
        if row["status"] == ST_SKIPPED and row.get("outcome") == "skipped":
            skipped += 1
        facts = _json_obj(row.get("facts_json"))
        out_steps.append(
            WorkflowStepRun(
                step_id=sid,
                pipeline=str(row["pipeline"]),
                status=str(row["status"]),
                job_id=row.get("job_id"),
                instance_id=job.instance_id if job is not None else None,
                input_address=row.get("input_address"),
                output_address=row.get("output_address"),
                pre_existing_tables=json.loads(pre) if pre else None,
                backup_address=row.get("backup_address"),
                snapshot_job_id=row.get("snapshot_job_id"),
                rollback_job_id=row.get("rollback_job_id"),
                rollback_action=row.get("rollback_action"),
                error=row.get("error"),
                started_at=row.get("started_at"),
                ended_at=row.get("ended_at"),
                outcome=row.get("outcome"),
                reason=row.get("reason"),
                facts=facts,
                probe_job_id=row.get("probe_job_id"),
            )
        )
    return WorkflowRunDetail(
        run_id=str(run["run_id"]),
        workflow=str(run["workflow"]),
        trigger=str(run["trigger"]),
        batch_id=run.get("batch_id"),
        status=str(run["status"]),
        created_at=str(run["created_at"]),
        started_at=run.get("started_at"),
        ended_at=run.get("ended_at"),
        error=run.get("error"),
        created_by=str(run.get("created_by") or "web"),
        definition=definition if with_definition else None,
        steps=out_steps,
        edges=edge_out,
        handled_failures=handled,
        skipped=skipped,
    )


def get_run(run_id: str) -> WorkflowRunDetail | None:
    """Return one workflow run with its step states, or ``None``."""
    web_db.ensure_schema()
    run = _run_row(run_id)
    return _to_detail(run) if run is not None else None


def list_runs(
    *, workflow: str | None = None, limit: int = 50, offset: int = 0
) -> WorkflowRunListResponse:
    """Return workflow runs, newest first."""
    web_db.ensure_schema()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = "workspace_id = ?" + (" AND workflow = ?" if workflow else "")
    params: tuple[Any, ...] = (active_workspace(), workflow) if workflow else (active_workspace(),)
    with web_db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM web_workflow_runs WHERE " + where, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM web_workflow_runs WHERE "
            + where
            + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return WorkflowRunListResponse(
        total=int(total), runs=[_to_detail(dict(r), with_definition=False) for r in rows]
    )


def active_run(workflow: str) -> str | None:
    """Return the id of the workflow's in-progress run, or ``None``."""
    web_db.ensure_schema()
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT run_id FROM web_workflow_runs WHERE workflow = ? AND workspace_id = ? "
            "AND status IN (" + placeholders + ") ORDER BY created_at DESC LIMIT 1",
            (workflow, active_workspace(), *sorted(ACTIVE_RUN_STATUSES)),
        ).fetchone()
    return str(row["run_id"]) if row is not None else None


# ---------------------------------------------------------------------------
# Start / cancel / retry
# ---------------------------------------------------------------------------


def start_run(
    workflow: WorkflowDetail,
    *,
    trigger: str,
    actor: str = "web",
    root_input: str | None = None,
    batch_id: str | None = None,
) -> str:
    """Create a workflow run and start its root steps.

    Args:
        workflow: The workflow to run (its current definition is frozen).
        trigger: ``manual`` / ``cron`` / ``interval`` / ``watch``.
        actor: Audit label.
        root_input: Address to read for the root step(s) (watch batch or a
            manual override). ``None`` keeps each root step's own input.
        batch_id: Display label (watch batch folder).

    Returns:
        The new run id.

    Raises:
        WorkflowRunError: When the workflow is invalid or already running.
    """
    web_db.ensure_schema()
    if active_run(workflow.name) is not None:
        raise WorkflowRunError(
            f"Workflow '{workflow.name}' already has a run in progress; wait for it to finish."
        )
    definition = WorkflowWrite(
        description=workflow.description,
        enabled=workflow.enabled,
        trigger=workflow.trigger,
        steps=workflow.steps,
        edges=workflow.edges,
    )
    check = workflows_svc.validate(definition, check_trigger=False)
    if not check.ok:
        raise WorkflowRunError("Workflow is not valid: " + "; ".join(check.errors))
    run_id = uuid.uuid4().hex[:16]
    now = _now_iso()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_workflow_runs (run_id, workflow, trigger, batch_id, root_input, "
            "status, cancel_requested, created_at, created_by, started_at, definition_json, "
            "workspace_id) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                run_id,
                workflow.name,
                trigger,
                batch_id,
                root_input,
                RUN_RUNNING,
                now,
                actor,
                now,
                definition.model_dump_json(by_alias=True),
                active_workspace(),
            ),
        )
        for step in definition.steps:
            conn.execute(
                "INSERT INTO web_workflow_steps (run_id, step_id, pipeline, status) "
                "VALUES (?, ?, ?, ?)",
                (run_id, step.id, step.pipeline, ST_WAITING),
            )
    workflows_svc.record_fire(workflow.name, run_id=run_id)
    web_db.audit(
        actor=actor,
        action="workflow.run",
        target=workflow.name,
        detail_json=json.dumps({"run_id": run_id, "trigger": trigger, "root_input": root_input}),
    )
    logger.info("Workflow %s started run %s (trigger=%s)", workflow.name, run_id, trigger)
    advance_run(run_id)
    return run_id


def cancel_run(run_id: str, *, actor: str = "web") -> WorkflowRunDetail:
    """Stop a running workflow; finished steps are rolled back.

    Raises:
        WorkflowRunError: When the run is unknown or already finished.
    """
    web_db.ensure_schema()
    run = _run_row(run_id)
    if run is None:
        raise WorkflowRunError(f"Workflow run '{run_id}' not found.")
    if run["status"] not in ACTIVE_RUN_STATUSES:
        raise WorkflowRunError(f"Workflow run '{run_id}' is already {run['status']}.")
    _update_run(run_id, cancel_requested=1)
    web_db.audit(
        actor=actor, action="workflow.cancel", target=str(run["workflow"]), detail_json=run_id
    )
    advance_run(run_id)
    out = get_run(run_id)
    assert out is not None
    return out


def retry_rollback(run_id: str, *, actor: str = "web") -> WorkflowRunDetail:
    """Re-run the failed rollback actions of a ``rollback_failed`` run.

    Raises:
        WorkflowRunError: When the run is not ``rollback_failed``.
    """
    web_db.ensure_schema()
    run = _run_row(run_id)
    if run is None:
        raise WorkflowRunError(f"Workflow run '{run_id}' not found.")
    if run["status"] != RUN_ROLLBACK_FAILED:
        raise WorkflowRunError(f"Workflow run '{run_id}' is {run['status']}, not rollback_failed.")
    for sid, row in _step_rows(run_id).items():
        if row["status"] == ST_ROLLBACK_FAILED:
            _update_step(run_id, sid, status=ST_ROLLING_BACK, rollback_job_id=None, error=None)
    _update_run(run_id, status=RUN_ROLLING_BACK, ended_at=None)
    web_db.audit(
        actor=actor,
        action="workflow.rollback_retry",
        target=str(run["workflow"]),
        detail_json=run_id,
    )
    advance_run(run_id)
    out = get_run(run_id)
    assert out is not None
    return out


# ---------------------------------------------------------------------------
# Advance
# ---------------------------------------------------------------------------


def advance_all() -> None:
    """Advance every active workflow run by one step (called each worker tick)."""
    web_db.ensure_schema()
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, workspace_id FROM web_workflow_runs WHERE status IN ("
            + placeholders
            + ")",
            tuple(sorted(ACTIVE_RUN_STATUSES)),
        ).fetchall()
    for row in rows:
        # Each run advances inside its own workspace: its steps resolve
        # that workspace's pipelines / addresses and submit jobs there.
        with use_workspace(str(row["workspace_id"] or DEFAULT_WORKSPACE)):
            try:
                advance_run(str(row["run_id"]))
            except Exception:
                logger.exception("Advancing workflow run %s failed.", row["run_id"])


def advance_run(run_id: str) -> None:
    """Move one run forward as far as it can go without waiting on a job."""
    for _ in range(10):  # bounded: each pass either changes state or stops
        run = _run_row(run_id)
        if run is None or run["status"] not in ACTIVE_RUN_STATUSES:
            return
        before = (run["status"], json.dumps(_step_rows(run_id), sort_keys=True, default=str))
        status = str(run["status"])
        if status == RUN_RUNNING:
            _advance_running(run)
        elif status == RUN_STOPPING:
            _advance_stopping(run)
        elif status == RUN_ROLLING_BACK:
            _advance_rollback(run)
        elif status == RUN_FINISHING:
            _advance_finishing(run)
        after_run = _run_row(run_id)
        after = (
            (after_run or {}).get("status"),
            json.dumps(_step_rows(run_id), sort_keys=True, default=str),
        )
        if after == before:
            return


def _sync_step_jobs(
    run_id: str, steps: dict[str, dict[str, Any]], definition: WorkflowWrite | None = None
) -> None:
    """Fold job outcomes into step statuses."""
    probes = {s.id: bool(s.probes) for s in (definition.steps if definition else [])}
    for sid, row in steps.items():
        st = row["status"]
        if st == ST_STARTING:
            # Another process is starting this step right now. A claim older
            # than a minute means that process died between claim and queue:
            # release it so the step is retried.
            claimed = row.get("started_at") or ""
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(claimed)).total_seconds()
            except ValueError:
                age = 0.0
            if claimed and age > 60:
                _claim_step(run_id, sid, ST_STARTING, ST_WAITING)
                row["status"] = ST_WAITING
            continue
        if st == ST_SNAPSHOTTING:
            js = _job_status(row.get("snapshot_job_id"))
            if js == jobs_svc.STATUS_DONE:
                _update_step(
                    run_id, sid, status=ST_WAITING, backup_address=row.get("output_address")
                )
                row["status"] = ST_WAITING
                row["backup_address"] = row.get("output_address")
                row["_snapshot_ready"] = True
            elif js == jobs_svc.STATUS_CANCELLED:
                _update_step(run_id, sid, status=ST_CANCELLED, ended_at=_now_iso())
                row["status"] = ST_CANCELLED
            elif js == jobs_svc.STATUS_FAILED or js is None:
                err = _job_error(row.get("snapshot_job_id")) or "backup failed"
                _update_step(
                    run_id,
                    sid,
                    status=ST_FAILED,
                    error=f"backup before run failed: {err}",
                    ended_at=_now_iso(),
                )
                row["status"] = ST_FAILED
        elif st in (ST_QUEUED, ST_RUNNING):
            js = _job_status(row.get("job_id"))
            mapped = {
                jobs_svc.STATUS_QUEUED: ST_QUEUED,
                jobs_svc.STATUS_RUNNING: ST_RUNNING,
                jobs_svc.STATUS_DONE: ST_DONE,
                jobs_svc.STATUS_FAILED: ST_FAILED,
                jobs_svc.STATUS_CANCELLED: ST_CANCELLED,
            }.get(js or "", ST_FAILED if js is None else st)
            if mapped == ST_DONE and probes.get(sid):
                # The step's own job finished; its data probes run next.
                mapped = ST_PROBING
            if mapped != st:
                fields: dict[str, Any] = {"status": mapped}
                if mapped in (ST_DONE, ST_FAILED, ST_CANCELLED):
                    fields["ended_at"] = _now_iso()
                if mapped == ST_FAILED:
                    fields["error"] = _job_error(row.get("job_id")) or "job failed"
                _update_step(run_id, sid, **fields)
                row.update(fields)
        elif st == ST_PROBING and row.get("probe_job_id") not in (None, "", _PROBE_CLAIM):
            pj = str(row["probe_job_id"])
            js = _job_status(pj)
            if js in (jobs_svc.STATUS_QUEUED, jobs_svc.STATUS_RUNNING):
                continue
            fields = {"ended_at": _now_iso()}
            if js == jobs_svc.STATUS_DONE:
                fields["status"] = ST_DONE
            elif js == jobs_svc.STATUS_CANCELLED:
                fields["status"] = ST_CANCELLED
            else:
                job = jobs_svc.get_job(pj)
                msg = ((job.result or {}).get("message") if job else None) or _job_error(pj)
                fields["status"] = ST_FAILED
                fields["error"] = f"data probe failed: {msg or 'probe job failed'}"
            _update_step(run_id, sid, **fields)
            row.update(fields)


#: Placeholder written to ``probe_job_id`` while one process submits the probe job.
_PROBE_CLAIM = "-"

#: Step statuses after which the step's outgoing links can be decided.
_SETTLED = frozenset({ST_DONE, ST_FAILED, ST_ROLLED_BACK, ST_SKIPPED})


def _settled(row: dict[str, Any]) -> bool:
    """A step whose links are decided and that does no more work in this phase."""
    return row["status"] in _SETTLED and _decisions(row) is not None


def _edge_taken(row: dict[str, Any], edge: WorkflowEdge) -> bool:
    decided = _decisions(row) or {}
    return bool(((decided.get("edges") or {}).get(edge.target) or {}).get("taken"))


def _handled(row: dict[str, Any]) -> bool:
    return bool((_decisions(row) or {}).get("handled"))


def _final_ok(row: dict[str, Any]) -> bool:
    """A step that lets the run succeed: done, skipped, or a handled failure."""
    if not _settled(row):
        return False
    if row["status"] in (ST_DONE, ST_SKIPPED):
        return True
    return _handled(row) and row["status"] in (ST_FAILED, ST_ROLLED_BACK)


def _claim_probe(run_id: str, step_id: str) -> bool:
    with web_db.connect() as conn:
        cur = conn.execute(
            "UPDATE web_workflow_steps SET probe_job_id = ? WHERE run_id = ? AND step_id = ? "
            "AND status = ? AND (probe_job_id IS NULL OR probe_job_id = '')",
            (_PROBE_CLAIM, run_id, step_id, ST_PROBING),
        )
        return cur.rowcount == 1


def _start_probe(run: dict[str, Any], step: WorkflowStep, row: dict[str, Any]) -> None:
    """Queue the ``workflow_probe`` job of a step whose own job just succeeded."""
    run_id = str(run["run_id"])
    if not _claim_probe(run_id, step.id):
        return
    try:
        job = jobs_svc.submit_workflow_probe(
            address=str(row.get("output_address") or ""),
            probes=[p.model_dump(exclude_none=True) for p in step.probes],
            label=f"workflow:{run['workflow']}:probe",
            batch_id=str(run.get("batch_id") or run_id),
            workflow_run_id=run_id,
            workflow_step_id=step.id,
        )
    except jobs_svc.SubmitError as exc:
        _update_step(
            run_id, step.id, status=ST_FAILED, error=f"data probe failed: {exc}", probe_job_id=None
        )
        return
    _update_step(run_id, step.id, probe_job_id=job.job_id)


# ---------------------------------------------------------------------------
# Facts + link decisions (branching)
# ---------------------------------------------------------------------------


def _duration_s(job: Any) -> float | None:
    try:
        start = datetime.fromisoformat(str(job.started_at))
        end = datetime.fromisoformat(str(job.ended_at))
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds(), 3)


def _gather_facts(
    run: dict[str, Any], row: dict[str, Any], outcome: str, groups: set[str]
) -> dict[str, Any]:
    """Collect the facts one settled step's outgoing conditions read.

    Only the groups a condition uses are gathered (the output listing is
    a brokered probe, the timeline a log scan). Every source is metadata —
    statuses, counters, table names, aggregate numbers — never row values.
    A source that is missing or fails leaves its facts unknown.
    """
    job = jobs_svc.get_job(str(row["job_id"])) if row.get("job_id") else None
    iid = job.instance_id if job is not None else None
    facts: dict[str, Any] = {
        "status": outcome,
        "error": str(row.get("error") or ""),
        "duration_s": _duration_s(job) if job is not None else None,
        "pipeline": str(row.get("pipeline") or ""),
        "input_address": str(row.get("input_address") or ""),
        "output_address": str(row.get("output_address") or ""),
        "batch_id": str(run.get("batch_id") or ""),
        "root_input": str(run.get("root_input") or ""),
        "trigger": str(run.get("trigger") or ""),
    }
    if "timeline" in groups and iid:
        with contextlib.suppress(Exception):
            from ovid2.web.services import timeline_svc

            tl = timeline_svc.build_timeline(iid)
            tables = {t.table: t.status for t in tl.tables}
            facts["tables"] = tables
            facts["tables_completed"] = [t for t, st in tables.items() if st == "completed"]
            facts["tables_failed"] = [t for t, st in tables.items() if st == "failed"]
            facts["tables_completed_count"] = len(facts["tables_completed"])
            facts["tables_failed_count"] = len(facts["tables_failed"])
            facts["warnings"] = tl.warning_count
            facts["errors"] = tl.error_count
            facts["fixes_applied"] = sum(1 for f in tl.fixes if f.status == "applied")
    if "health" in groups:
        facts["health"] = {"available": False}
        if iid:
            with contextlib.suppress(Exception):
                from ovid2.web.services import health_report_svc

                report = health_report_svc.get_report(iid)
                if report:
                    summary = report.get("summary") or {}
                    checks = {
                        str(c.get("id")): str(c.get("status") or "")
                        for c in report.get("checks") or []
                    }
                    total = int(summary.get("checks_total") or 0)
                    passed = int(summary.get("checks_passed") or 0)
                    failed = int(summary.get("checks_exception") or 0) + int(
                        summary.get("checks_error") or 0
                    )
                    facts["health"] = {
                        "available": True,
                        "checks_total": total,
                        "checks_passed": passed,
                        "checks_failed": failed,
                        "pass_rate": round(100.0 * passed / total, 2) if total else None,
                        "blockers": int(summary.get("blockers") or 0),
                        "failed_by_severity": dict(summary.get("failed_by_severity") or {}),
                        "checks": checks,
                        "failed_checks": [
                            c for c, st in checks.items() if st in ("exception", "error")
                        ],
                    }
    if "output" in groups and row.get("output_address"):
        with contextlib.suppress(Exception):
            from ovid2.web.services import output_probe_svc

            probe = output_probe_svc.existing_output_tables(str(row["output_address"]))
            facts["output"] = {"tables": list(probe.tables) if probe.known else None}
    if "analysis" in groups and iid:
        with contextlib.suppress(Exception):
            from ovid2.reports.artifacts import analysis_artifact_path, read_analysis_records

            rows: dict[str, int] = {}
            for rec in read_analysis_records(analysis_artifact_path(iid)):
                if rec.get("type") == "distinct" and rec.get("table"):
                    t = str(rec["table"])
                    rows[t] = max(rows.get(t, 0), int(rec.get("total_rows") or 0))
            facts["rows"] = rows
    if "metrics" in groups and iid:
        with contextlib.suppress(Exception):
            from ovid2.web.services import metrics_svc

            sample = metrics_svc.latest(iid) or {}
            value = sample.get("rows_written_total")
            facts["rows_written_total"] = int(value) if value is not None else None
    if row.get("probe_job_id") and row["probe_job_id"] != _PROBE_CLAIM:
        probe_job = jobs_svc.get_job(str(row["probe_job_id"]))
        results = ((probe_job.result or {}).get("results") if probe_job else None) or {}
        facts["query"] = {str(k): v for k, v in results.items()}
    return facts


def _decide(
    run: dict[str, Any],
    definition: WorkflowWrite,
    step: WorkflowStep,
    row: dict[str, Any],
    outcome: str,
) -> None:
    """Decide every outgoing link of a settled step and store the result.

    A failed step whose ``failure`` / ``always`` link is taken is *handled*:
    its own output is rolled back (when it ran) before the failure path
    continues, instead of the whole workflow stopping.
    """
    run_id = str(run["run_id"])
    out_edges = [e for e in definition.edges if e.source == step.id]
    groups: set[str] = set()
    for e in out_edges:
        if e.when:
            groups |= wc.field_groups(e.when)
    facts = _gather_facts(run, row, outcome, groups)
    edges: dict[str, dict[str, Any]] = {}
    matched_conditional = False
    for e in out_edges:
        if e.on == "otherwise":
            continue
        if e.on == "success" and outcome != "succeeded":
            taken, reason = False, "step failed"
        elif e.on == "failure" and outcome != "failed":
            taken, reason = False, "step succeeded"
        elif e.when:
            taken, reason = wc.evaluate(e.when, facts), wc.explain(e.when, facts)
        else:
            taken, reason = True, f"on {e.on}"
        if taken and e.on == "success" and e.when:
            matched_conditional = True
        edges[e.target] = {"taken": taken, "reason": reason}
    for e in out_edges:
        if e.on != "otherwise":
            continue
        if outcome != "succeeded":
            edges[e.target] = {"taken": False, "reason": "step failed"}
        elif matched_conditional:
            edges[e.target] = {"taken": False, "reason": "another branch matched"}
        else:
            edges[e.target] = {"taken": True, "reason": "no other branch matched"}
    handled = outcome == "failed" and any(
        edges.get(e.target, {}).get("taken") for e in out_edges if e.on in ("failure", "always")
    )
    fields: dict[str, Any] = {
        "outcome": outcome,
        "facts_json": json.dumps(facts, default=str),
        "decisions_json": json.dumps({"edges": edges, "handled": handled}),
    }
    if handled:
        targets = [t for t, d in edges.items() if d["taken"]]
        fields["reason"] = "failure handled by " + ", ".join(targets)
        actions, err = _rollback_plan(run, step, row)
        if err:
            fields.update(status=ST_ROLLBACK_FAILED, error=f"{row.get('error') or ''}; {err}")
        elif actions:
            fields.update(
                status=ST_ROLLING_BACK, rollback_plan_json=json.dumps(actions), rollback_job_id=None
            )
    _update_step(run_id, step.id, **fields)
    row.update(fields)
    logger.info(
        "Workflow run %s step %s %s; links: %s",
        run_id,
        step.id,
        outcome,
        {t: d["taken"] for t, d in edges.items()},
    )


def _skip_step(
    run_id: str, definition: WorkflowWrite, step_id: str, reason: str, row: dict[str, Any]
) -> None:
    """Skip a step on a branch that was not taken; none of its links are taken."""
    edges = {
        e.target: {"taken": False, "reason": "upstream step skipped"}
        for e in definition.edges
        if e.source == step_id
    }
    fields = {
        "status": ST_SKIPPED,
        "outcome": "skipped",
        "reason": reason,
        "decisions_json": json.dumps({"edges": edges, "handled": False}),
        "ended_at": _now_iso(),
    }
    with web_db.connect() as conn:
        cur = conn.execute(
            "UPDATE web_workflow_steps SET status = ?, outcome = ?, reason = ?, "
            "decisions_json = ?, ended_at = ? WHERE run_id = ? AND step_id = ? AND status = ?",
            (
                ST_SKIPPED,
                "skipped",
                reason,
                fields["decisions_json"],
                fields["ended_at"],
                run_id,
                step_id,
                ST_WAITING,
            ),
        )
    if cur.rowcount == 1:
        row.update(fields)


def _join_reason(step: WorkflowStep, incoming: list[WorkflowEdge], steps: dict[str, Any]) -> str:
    not_taken = [e.source for e in incoming if not _edge_taken(steps[e.source], e)]
    if step.join == "any":
        return "no incoming link was taken (" + ", ".join(not_taken) + ")"
    return "link not taken from " + ", ".join(not_taken)


def _advance_running(run: dict[str, Any]) -> None:
    run_id = str(run["run_id"])
    definition = _definition(run)
    steps = _step_rows(run_id)
    _sync_step_jobs(run_id, steps, definition)
    by_id = {s.id: s for s in definition.steps}
    order = workflows_svc.topological_order(definition.steps, definition.edges) or []

    if run.get("cancel_requested"):
        _update_run(run_id, status=RUN_STOPPING, error="cancelled by operator")
        return

    # Data probes of steps whose job just succeeded.
    for sid in order:
        row = steps[sid]
        if row["status"] == ST_PROBING and not row.get("probe_job_id"):
            _start_probe(run, by_id[sid], row)

    # Decide the outgoing links of every newly settled step.
    for sid in order:
        row = steps[sid]
        if _decisions(row) is not None:
            continue
        if row["status"] == ST_DONE:
            _decide(run, definition, by_id[sid], row, "succeeded")
        elif row["status"] == ST_FAILED:
            _decide(run, definition, by_id[sid], row, "failed")

    unhandled = [
        sid
        for sid in order
        if steps[sid]["status"] == ST_CANCELLED
        or steps[sid]["status"] == ST_ROLLBACK_FAILED
        or (steps[sid]["status"] == ST_FAILED and not _handled(steps[sid]))
    ]
    if unhandled:
        bad = steps[unhandled[0]]
        reason = f"step '{unhandled[0]}' {bad['status']}: " + str(bad.get("error") or "")
        _update_run(run_id, status=RUN_STOPPING, error=reason.strip().rstrip(":"))
        return

    # Roll back handled failures (one storage job at a time).
    if any(r["status"] == ST_ROLLING_BACK for r in steps.values()):
        _advance_plans(run, ST_ROLLING_BACK, ST_ROLLED_BACK)
        steps = _step_rows(run_id)

    if all(_final_ok(r) for r in steps.values()):
        _begin_finishing(run_id, steps)
        return

    for sid in order:
        row = steps[sid]
        if row["status"] != ST_WAITING:
            continue
        step = by_id[sid]
        incoming = [e for e in definition.edges if e.target == sid]
        if not all(_settled(steps[e.source]) for e in incoming):
            continue
        if incoming:
            taken = [_edge_taken(steps[e.source], e) for e in incoming]
            go = any(taken) if step.join == "any" else all(taken)
            if not go:
                _skip_step(run_id, definition, sid, _join_reason(step, incoming, steps), row)
                continue
        if not _claim_step(run_id, sid, ST_WAITING, ST_STARTING):
            continue
        if not row.get("started_at"):
            _update_step(run_id, sid, started_at=_now_iso())
        try:
            _start_step(run, definition, step, row, steps)
        except Exception as exc:
            logger.exception("Starting workflow step %s/%s failed.", run_id, sid)
            _fail_step(run_id, sid, f"could not start the step: {exc}")
            continue
        # A step that neither failed nor moved on (snapshot / queued) is
        # released back to waiting so the next pass retries it.
        _claim_step(run_id, sid, ST_STARTING, ST_WAITING)


def _resolve_io(
    run: dict[str, Any],
    definition: WorkflowWrite,
    step: WorkflowStep,
    steps: dict[str, dict[str, Any]],
    detail: Any,
) -> tuple[str, str]:
    """Return ``(input, output)``; a taken data link supplies the input."""
    data_in = [e for e in definition.edges if e.target == step.id and e.type == "data"]
    taken = [e.source for e in data_in if _edge_taken(steps[e.source], e)]
    if taken:
        inp = str(steps[taken[0]].get("output_address") or "")
    elif data_in:
        inp = str(step.overrides.input_address or detail.input_address or "")
    else:
        roots = workflows_svc.root_steps(definition.steps, definition.edges)
        if step.id in roots and run.get("root_input"):
            inp = str(run["root_input"])
        else:
            inp = str(step.overrides.input_address or detail.input_address or "")
    out = str(step.overrides.output_address or detail.output_address or "")
    return inp, out


def _fail_step(run_id: str, step_id: str, message: str) -> None:
    _update_step(run_id, step_id, status=ST_FAILED, error=message, ended_at=_now_iso())


def _start_step(
    run: dict[str, Any],
    definition: WorkflowWrite,
    step: WorkflowStep,
    row: dict[str, Any],
    steps: dict[str, dict[str, Any]],
) -> None:
    """Snapshot (when needed) then queue the step's job."""
    from ovid2.web.services import output_probe_svc

    run_id = str(run["run_id"])
    detail = pipelines_svc.get_pipeline(step.pipeline)
    if detail is None:
        _fail_step(run_id, step.id, f"pipeline '{step.pipeline}' no longer exists.")
        return
    inp, out = _resolve_io(run, definition, step, steps, detail)
    if not inp or not out:
        _fail_step(run_id, step.id, "the step has no input or no output address.")
        return
    allow = (
        step.overrides.allow_overwrite
        if step.overrides.allow_overwrite is not None
        else detail.allow_overwrite
    )
    if not row.get("pre_existing_tables_json"):
        probe = output_probe_svc.existing_output_tables(out)
        if probe.known:
            pre = list(probe.tables)
        elif step.rollback:
            _fail_step(
                run_id,
                step.id,
                f"cannot list what is already at output '{out}' ({probe.message or 'unknown'}), "
                "so it could not be rolled back; turn rollback off for this step to run it.",
            )
            return
        else:
            pre = []
        if pre and not allow:
            _fail_step(
                run_id,
                step.id,
                f"output '{out}' already holds {len(pre)} table(s); allow overwrite on this step "
                "(or its pipeline) to replace them.",
            )
            return
        _update_step(
            run_id,
            step.id,
            input_address=inp,
            output_address=out,
            pre_existing_tables_json=json.dumps(pre),
            started_at=_now_iso(),
        )
        row["pre_existing_tables_json"] = json.dumps(pre)
        if pre and step.rollback:
            snap = jobs_svc.submit_workflow_storage(
                action="snapshot",
                address=out,
                tables=pre,
                key=run_id,
                label=f"workflow:{run['workflow']}:backup",
                batch_id=str(run.get("batch_id") or run_id),
                workflow_run_id=run_id,
                workflow_step_id=step.id,
            )
            _update_step(run_id, step.id, status=ST_SNAPSHOTTING, snapshot_job_id=snap.job_id)
            return
    tables = step.overrides.tables
    try:
        request = pipelines_svc._to_submit_request(
            detail,
            batch_id=run.get("batch_id"),
            input_address=inp,
            output_address=out,
            tables=list(tables) if tables else None,
            confirm_overwrite=bool(allow),
        )
        response = jobs_svc.submit(request, actor=f"workflow:{run['workflow']}")
    except jobs_svc.OverwriteRequired as exc:
        _fail_step(run_id, step.id, str(exc))
        return
    except (jobs_svc.SubmitError, pipelines_svc.PipelineError, ValueError) as exc:
        _fail_step(run_id, step.id, f"could not submit the job: {exc}")
        return
    jobs_svc.tag_workflow_job(response.job_id, run_id=run_id, step_id=step.id)
    _update_step(
        run_id,
        step.id,
        status=ST_QUEUED,
        job_id=response.job_id,
        input_address=inp,
        output_address=out,
    )
    logger.info("Workflow run %s step %s queued job %s", run_id, step.id, response.job_id)


def _advance_stopping(run: dict[str, Any]) -> None:
    run_id = str(run["run_id"])
    steps = _step_rows(run_id)
    _sync_step_jobs(run_id, steps, _definition(run))
    active = False
    for sid, row in steps.items():
        st = row["status"]
        if st == ST_WAITING:
            _update_step(run_id, sid, status=ST_SKIPPED)
        elif st in _STEP_ACTIVE:
            active = True
            job_id = (
                row.get("snapshot_job_id")
                if st == ST_SNAPSHOTTING
                else row.get("probe_job_id")
                if st == ST_PROBING
                else row.get("job_id")
            )
            if job_id == _PROBE_CLAIM:
                job_id = None
            if job_id and _job_status(job_id) in (jobs_svc.STATUS_QUEUED, jobs_svc.STATUS_RUNNING):
                job = jobs_svc.get_job(job_id)
                if job is not None and not job.cancel_requested:
                    with contextlib.suppress(jobs_svc.SubmitError):
                        jobs_svc.cancel(job_id, actor="workflow")
    if not active:
        _begin_rollback(run)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def _rollback_plan(
    run: dict[str, Any], step: WorkflowStep, row: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(actions, error)`` that undo one step's effect on its output."""
    from ovid2.web.services import output_probe_svc

    out = str(row.get("output_address") or "")
    backup = bool(row.get("backup_address"))
    # A snapshot job that started (even one that failed half-way) may have
    # left a partial backup behind: always clean it up.
    snap_started = bool(row.get("snapshot_job_id"))
    ran = bool(row.get("job_id"))
    actions: list[dict[str, Any]] = []
    if ran and step.rollback and out:
        pre = json.loads(row.get("pre_existing_tables_json") or "[]")
        probe = output_probe_svc.existing_output_tables(out)
        if not probe.known:
            return [], f"cannot list output '{out}' to roll it back ({probe.message or 'unknown'})."
        pre_lower = {str(t).lower() for t in pre}
        new = [t for t in probe.tables if t.lower() not in pre_lower]
        if new:
            actions.append({"action": "drop", "tables": new})
        if backup and pre:
            actions.append({"action": "restore", "tables": pre})
    if snap_started:
        actions.append({"action": "drop-backup", "tables": []})
    del run
    return actions, None


def _begin_rollback(run: dict[str, Any]) -> None:
    run_id = str(run["run_id"])
    definition = _definition(run)
    steps = _step_rows(run_id)
    by_id = {s.id: s for s in definition.steps}
    for sid, row in steps.items():
        if not (row.get("job_id") or row.get("snapshot_job_id")):
            continue
        if row["status"] in (ST_ROLLED_BACK, ST_ROLLING_BACK):
            # A handled failure already undone (or being undone) on its branch.
            continue
        actions, err = _rollback_plan(run, by_id[sid], row)
        if err:
            _update_step(run_id, sid, status=ST_ROLLBACK_FAILED, error=err)
            continue
        if actions:
            _update_step(
                run_id,
                sid,
                status=ST_ROLLING_BACK,
                rollback_plan_json=json.dumps(actions),
                rollback_job_id=None,
            )
        elif row.get("job_id") and by_id[sid].rollback:
            _update_step(run_id, sid, status=ST_ROLLED_BACK, rollback_action="nothing to undo")
    _update_run(run_id, status=RUN_ROLLING_BACK)
    logger.info("Workflow run %s rolling back: %s", run_id, run.get("error"))


def _advance_plans(run: dict[str, Any], active_status: str, ok_status: str) -> bool | None:
    """Drive per-step action plans one job at a time (reverse topological order).

    Returns ``None`` while work remains, ``True`` when every plan finished,
    ``False`` when one failed.
    """
    run_id = str(run["run_id"])
    definition = _definition(run)
    steps = _step_rows(run_id)
    order = list(
        reversed(workflows_svc.topological_order(definition.steps, definition.edges) or [])
    )
    failed = any(r["status"] == ST_ROLLBACK_FAILED for r in steps.values())
    for sid in order:
        row = steps[sid]
        if row["status"] != active_status:
            continue
        plan: list[dict[str, Any]] = json.loads(row.get("rollback_plan_json") or "[]")
        job_id = row.get("rollback_job_id")
        if job_id:
            js = _job_status(job_id)
            if js in (jobs_svc.STATUS_QUEUED, jobs_svc.STATUS_RUNNING):
                return None
            if js != jobs_svc.STATUS_DONE:
                err = _job_error(job_id) or "rollback job failed"
                _update_step(
                    run_id,
                    sid,
                    status=ST_ROLLBACK_FAILED if active_status == ST_ROLLING_BACK else ST_DONE,
                    error=f"{plan[0]['action'] if plan else 'rollback'} failed: {err}",
                    rollback_job_id=None,
                )
                if active_status == ST_ROLLING_BACK:
                    failed = True
                continue
            plan = plan[1:]
            _update_step(run_id, sid, rollback_plan_json=json.dumps(plan), rollback_job_id=None)
        if not plan:
            _update_step(run_id, sid, status=ok_status, rollback_action=None)
            continue
        nxt = plan[0]
        job = jobs_svc.submit_workflow_storage(
            action=str(nxt["action"]),
            address=str(row["output_address"]),
            tables=list(nxt.get("tables") or []),
            key=run_id,
            label=f"workflow:{run['workflow']}:{nxt['action']}",
            batch_id=str(run.get("batch_id") or run_id),
            workflow_run_id=run_id,
            workflow_step_id=sid,
        )
        _update_step(run_id, sid, rollback_job_id=job.job_id, rollback_action=str(nxt["action"]))
        return None
    return not failed


def _advance_rollback(run: dict[str, Any]) -> None:
    done = _advance_plans(run, ST_ROLLING_BACK, ST_ROLLED_BACK)
    if done is None:
        return
    run_id = str(run["run_id"])
    if done:
        final = RUN_CANCELLED if run.get("cancel_requested") else RUN_ROLLED_BACK
    else:
        final = RUN_ROLLBACK_FAILED
    _update_run(run_id, status=final, ended_at=_now_iso())
    logger.info("Workflow run %s finished: %s", run_id, final)


def _begin_finishing(run_id: str, steps: dict[str, dict[str, Any]]) -> None:
    any_cleanup = False
    for sid, row in steps.items():
        if row.get("backup_address") and row["status"] == ST_DONE:
            any_cleanup = True
            _update_step(
                run_id,
                sid,
                status=ST_CLEANING,
                rollback_plan_json=json.dumps([{"action": "drop-backup", "tables": []}]),
                rollback_job_id=None,
            )
    if any_cleanup:
        _update_run(run_id, status=RUN_FINISHING)
    else:
        _update_run(run_id, status=RUN_SUCCEEDED, ended_at=_now_iso())
        logger.info("Workflow run %s succeeded", run_id)


def _advance_finishing(run: dict[str, Any]) -> None:
    done = _advance_plans(run, ST_CLEANING, ST_DONE)
    if done is None:
        return
    _update_run(str(run["run_id"]), status=RUN_SUCCEEDED, ended_at=_now_iso())
    logger.info("Workflow run %s succeeded", run["run_id"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web worker.")
