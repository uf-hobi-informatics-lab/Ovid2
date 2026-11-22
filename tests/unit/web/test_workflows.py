"""Workflows: definition service + runner state machine + disc rollback.

The runner is driven with a *simulated* worker: queued jobs are claimed
from the real ``web_jobs`` table; pipeline jobs are emulated (they write
their tables into the output folder, or fail on demand) while
``workflow_storage`` jobs run the real ``ovid2 tools workflow-storage``
entry against real disc folders — so snapshot / restore / drop are
exercised end-to-end on the filesystem.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from ovid2.web.models import (
    PipelineTrigger,
    PipelineWrite,
    WorkflowEdge,
    WorkflowStep,
    WorkflowStepOverrides,
    WorkflowWrite,
)
from ovid2.web.services import (
    jobs_svc,
    output_probe_svc,
    pipelines_svc,
    workflow_runner_svc,
    workflows_svc,
)

# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def env(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A disc storage account with folders a / b / c / d and copy pipelines."""
    root = tmp_path / "store"
    for sub in ("a", "b", "c", "d"):
        (root / sub).mkdir(parents=True)
    (root / "a" / "person.csv").write_text("id\n1\n", encoding="utf-8")
    (root / "a" / "visit.csv").write_text("id\n1\n", encoding="utf-8")
    connections = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (connections / "store.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(root)}}), encoding="utf-8"
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    for sub in ("a", "b", "c", "d"):
        (configs / f"addr_{sub}.json").write_text(
            json.dumps({"connection": "store", "location": {"path": sub}}), encoding="utf-8"
        )
    monkeypatch.setenv("OVID2_WORKFLOWS_DIR", str(isolated_xdg / "config" / "ovid2" / "workflows"))
    monkeypatch.setenv("OVID2_PIPELINES_DIR", str(isolated_xdg / "config" / "ovid2" / "pipelines"))

    # Output probe: list the disc folder directly (no broker subprocess).
    def _probe(address: str) -> output_probe_svc.OutputProbe:
        sub = address.removeprefix("addr_")
        folder = root / sub
        if not folder.is_dir():
            return output_probe_svc.OutputProbe(known=True, tables=[])
        names = sorted(
            p.name.split(".", 1)[0] for p in folder.iterdir() if not p.name.startswith(".")
        )
        return output_probe_svc.OutputProbe(known=True, tables=names)

    monkeypatch.setattr(output_probe_svc, "existing_output_tables", _probe)
    monkeypatch.setattr(jobs_svc, "check_overwrite", lambda *a, **k: [])
    monkeypatch.setattr(jobs_svc, "_expand_all_tables", lambda request: None)
    for name, inp, out in (
        ("p_ab", "addr_a", "addr_b"),
        ("p_bc", "", "addr_c"),
        ("p_d", "addr_a", "addr_d"),
    ):
        pipelines_svc.write_pipeline(
            name,
            PipelineWrite(
                kind="copy",
                pipeline=name,
                input_address=inp,
                output_address=out,
                tables=["person", "visit"],
                engine="duckdb",
                trigger=PipelineTrigger(type="manual"),
            ),
            actor="test",
            create_only=True,
        )
    return {"root": root}


def _chain(**over: Any) -> WorkflowWrite:
    """a → b (data) → c, and an independent d after the first step."""
    return WorkflowWrite(
        steps=[
            WorkflowStep(id="s1", pipeline="p_ab"),
            WorkflowStep(id="s2", pipeline="p_bc"),
            WorkflowStep(id="s3", pipeline="p_d", **over),
        ],
        edges=[
            WorkflowEdge(source="s1", target="s2", type="data"),
            WorkflowEdge(source="s1", target="s3", type="after"),
        ],
    )


class SimWorker:
    """Claims queued jobs and completes them synchronously."""

    def __init__(self, root: Path, fail_steps: set[str] | None = None) -> None:
        self.root = root
        self.fail_steps = fail_steps or set()
        self.ran: list[tuple[str, str]] = []

    def drain(self, rounds: int = 60) -> None:
        idle = 0
        for _ in range(rounds):
            workflow_runner_svc.advance_all()
            job = jobs_svc.claim_next_queued()
            if job is None:
                idle += 1
                if idle >= 2:
                    return
                continue
            idle = 0
            self._run(job)

    def _run(self, job: Any) -> None:
        kind = job.extra_args.get("kind")
        if kind == "workflow_storage":
            from ovid2.cli.tools import _workflow_storage_entry

            ns = argparse.Namespace(
                action=job.extra_args["action"],
                address=job.output_address,
                key=job.extra_args["key"],
                tables=list(job.tables),
            )
            rc = _workflow_storage_entry(ns)
            self.ran.append(("storage:" + ns.action, job.output_address))
            status = jobs_svc.STATUS_DONE if rc == 0 else jobs_svc.STATUS_FAILED
            jobs_svc.mark_terminal(job.job_id, status=status, error=None if rc == 0 else "boom")
            return
        self.ran.append((job.workflow_step_id or "", job.output_address))
        out = self.root / job.output_address.removeprefix("addr_")
        out.mkdir(parents=True, exist_ok=True)
        for t in job.tables:
            (out / f"{t}.csv").write_text(f"written by {job.workflow_step_id}\n", encoding="utf-8")
        if job.workflow_step_id in self.fail_steps:
            (out / "partial.csv").write_text("half\n", encoding="utf-8")
            jobs_svc.mark_terminal(job.job_id, status=jobs_svc.STATUS_FAILED, error="step blew up")
        else:
            jobs_svc.mark_terminal(job.job_id, status=jobs_svc.STATUS_DONE)


# --------------------------------------------------------------------------- definition


@pytest.mark.unit
def test_crud_duplicate_and_state(env: dict[str, Path]) -> None:
    res = workflows_svc.write_workflow("wf", _chain(), actor="t", create_only=True)
    assert res.action == "created"
    detail = workflows_svc.get_workflow("wf")
    assert detail is not None and detail.root_steps == ["s1"]
    with pytest.raises(workflows_svc.WorkflowError):
        workflows_svc.write_workflow("wf", _chain(), create_only=True)

    dup = workflows_svc.duplicate_workflow("wf")
    assert dup.name == "wf_copy"
    copy = workflows_svc.get_workflow("wf_copy")
    assert copy is not None and copy.enabled is False and len(copy.steps) == 3
    assert workflows_svc.duplicate_workflow("wf").name == "wf_copy2"

    names = [w.name for w in workflows_svc.list_workflows()]
    assert names == ["wf", "wf_copy", "wf_copy2"]
    workflows_svc.set_enabled("wf", False)
    assert workflows_svc.get_workflow("wf").enabled is False  # type: ignore[union-attr]
    workflows_svc.delete_workflow("wf_copy2")
    assert workflows_svc.get_workflow("wf_copy2") is None


@pytest.mark.unit
def test_validation_catches_graph_and_io_errors(env: dict[str, Path]) -> None:
    cyc = _chain()
    cyc.edges.append(WorkflowEdge(source="s2", target="s1", type="after"))
    assert "cycle" in " ".join(workflows_svc.validate(cyc).errors)

    missing = WorkflowWrite(steps=[WorkflowStep(id="x", pipeline="nope")])
    v = workflows_svc.validate(missing)
    assert not v.ok and "x" in v.step_errors

    # p_bc keeps its input open: alone, with no data edge, it has no input.
    lone = WorkflowWrite(steps=[WorkflowStep(id="only", pipeline="p_bc")])
    assert any("no input" in e for e in workflows_svc.validate(lone).errors)

    clash = WorkflowWrite(
        steps=[
            WorkflowStep(id="a1", pipeline="p_ab"),
            WorkflowStep(
                id="a2", pipeline="p_d", overrides=WorkflowStepOverrides(output_address="addr_b")
            ),
        ]
    )
    assert any("same output" in e for e in workflows_svc.validate(clash).errors)

    watch_two_roots = _chain()
    watch_two_roots.edges = []
    watch_two_roots.trigger = PipelineTrigger(type="watch")
    assert any("exactly one root" in e for e in workflows_svc.validate(watch_two_roots).errors)

    ok = workflows_svc.validate(_chain())
    assert ok.ok, ok.errors
    assert ok.order[0] == "s1"


# --------------------------------------------------------------------------- runner


@pytest.mark.unit
def test_successful_run_chains_data_and_order(env: dict[str, Path]) -> None:
    workflows_svc.write_workflow("wf", _chain(), create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    with pytest.raises(workflow_runner_svc.WorkflowRunError):
        workflow_runner_svc.start_run(wf, trigger="manual")
    sim = SimWorker(env["root"])
    sim.drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "succeeded", run
    steps = {s.step_id: s for s in run.steps}
    # Data edge: s2 read s1's output.
    assert steps["s2"].input_address == "addr_b"
    assert steps["s3"].input_address == "addr_a"
    assert all(s.status == "done" for s in run.steps)
    # s1 ran before both of its children.
    order = [sid for sid, _ in sim.ran if sid]
    assert order[0] == "s1"
    # Every step's job is tagged with the run.
    job = jobs_svc.get_job(steps["s2"].job_id or "")
    assert job is not None and job.workflow_run_id == run_id
    assert workflows_svc.get_workflow("wf").last_run_status == "succeeded"  # type: ignore[union-attr]


@pytest.mark.unit
def test_failure_rolls_back_new_and_overwritten_outputs(env: dict[str, Path]) -> None:
    root = env["root"]
    # addr_d already holds data the workflow will overwrite (allowed on the step).
    (root / "d" / "person.csv").write_text("ORIGINAL\n", encoding="utf-8")
    (root / "d" / "keep.csv").write_text("KEEP\n", encoding="utf-8")
    wf_def = _chain(overrides=WorkflowStepOverrides(allow_overwrite=True))
    # s3 must finish before s2 fails, so its overwrite has to be undone.
    wf_def.edges.append(WorkflowEdge(source="s3", target="s2", type="after"))
    workflows_svc.write_workflow("wf", wf_def, create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    SimWorker(root, fail_steps={"s2"}).drain()

    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "rolled_back", run.model_dump_json(indent=1)
    assert "s2" in (run.error or "")
    # s1 created b/* → dropped.  s2 created c/* (+partial) → dropped.
    assert sorted(p.name for p in (root / "b").iterdir()) == []
    assert sorted(p.name for p in (root / "c").iterdir()) == []
    # s3 overwrote d/person and created d/visit → restored / dropped; keep untouched.
    assert (root / "d" / "person.csv").read_text(encoding="utf-8") == "ORIGINAL\n"
    assert (root / "d" / "keep.csv").read_text(encoding="utf-8") == "KEEP\n"
    assert not (root / "d" / "visit.csv").exists()
    # Backup cleaned up.
    assert not (root / "d" / ".ovid2_wf_backup").exists()
    statuses = {s.step_id: s.status for s in run.steps}
    assert statuses["s1"] == "rolled_back" and statuses["s3"] == "rolled_back"


@pytest.mark.unit
def test_occupied_output_without_overwrite_fails_before_writing(env: dict[str, Path]) -> None:
    root = env["root"]
    (root / "b" / "person.csv").write_text("THERE\n", encoding="utf-8")
    workflows_svc.write_workflow("wf", _chain(), create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    SimWorker(root).drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "rolled_back"
    s1 = next(s for s in run.steps if s.step_id == "s1")
    assert s1.status == "failed" and "already holds" in (s1.error or "")
    assert s1.job_id is None
    assert (root / "b" / "person.csv").read_text(encoding="utf-8") == "THERE\n"
    assert {s.status for s in run.steps if s.step_id != "s1"} == {"skipped"}


@pytest.mark.unit
def test_successful_run_removes_backups(env: dict[str, Path]) -> None:
    root = env["root"]
    (root / "d" / "person.csv").write_text("OLD\n", encoding="utf-8")
    workflows_svc.write_workflow(
        "wf", _chain(overrides=WorkflowStepOverrides(allow_overwrite=True)), create_only=True
    )
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    sim = SimWorker(root)
    sim.drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "succeeded"
    assert ("storage:snapshot", "addr_d") in sim.ran
    assert ("storage:drop-backup", "addr_d") in sim.ran
    assert not (root / "d" / ".ovid2_wf_backup").exists()
    assert "written by s3" in (root / "d" / "person.csv").read_text(encoding="utf-8")


@pytest.mark.unit
def test_cancel_rolls_back_and_ends_cancelled(env: dict[str, Path]) -> None:
    workflows_svc.write_workflow("wf", _chain(), create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    # s1 queued but not run yet → cancel.
    workflow_runner_svc.cancel_run(run_id)
    SimWorker(env["root"]).drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "cancelled"
    assert workflow_runner_svc.active_run("wf") is None


@pytest.mark.unit
def test_failed_rollback_is_reported_and_retryable(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = env["root"]
    workflows_svc.write_workflow("wf", _chain(), create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    from ovid2.drivers.disc.driver import DiscDriver

    original = DiscDriver.drop_tables

    def _broken(self: DiscDriver, tables: list[str]) -> list[str]:
        raise PermissionError("read-only volume")

    monkeypatch.setattr(DiscDriver, "drop_tables", _broken)
    SimWorker(root, fail_steps={"s2"}).drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "rollback_failed"

    monkeypatch.setattr(DiscDriver, "drop_tables", original)
    workflow_runner_svc.retry_rollback(run_id)
    SimWorker(root).drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None and run.status == "rolled_back"
    assert list((root / "b").iterdir()) == []


@pytest.mark.unit
def test_worker_argv_for_storage_job(env: dict[str, Path]) -> None:
    from ovid2.web.worker import Worker

    resp = jobs_svc.submit_workflow_storage(
        action="restore",
        address="addr_d",
        tables=["person"],
        key="abc123",
        label="workflow:wf:restore",
        batch_id="b",
        workflow_run_id="abc123",
        workflow_step_id="s3",
    )
    job = jobs_svc.get_job(resp.job_id)
    assert job is not None
    argv = Worker(ovid2_bin=["ovid2"])._build_argv(job)
    assert argv == [
        "ovid2",
        "tools",
        "workflow-storage",
        "restore",
        "--address",
        "addr_d",
        "--key",
        "abc123",
        "--tables",
        "person",
    ]


# --------------------------------------------------------------------------- REST API


@pytest.fixture()
def api(env: dict[str, Path]) -> Any:
    from fastapi.testclient import TestClient

    from ovid2.web.app import create_app

    with TestClient(create_app()) as client:
        yield client


def _body() -> dict[str, Any]:
    return json.loads(_chain().model_dump_json(by_alias=True))


@pytest.mark.unit
def test_api_crud_duplicate_run_and_draft(api: Any) -> None:
    r = api.post("/api/v1/workflows/wf", json=_body())
    assert r.status_code == 201, r.text
    assert api.post("/api/v1/workflows/wf", json=_body()).status_code == 400
    listing = api.get("/api/v1/workflows").json()
    assert [w["name"] for w in listing] == ["wf"] and listing[0]["step_count"] == 3
    got = api.get("/api/v1/workflows/wf").json()
    assert got["root_steps"] == ["s1"] and got["edges"][0]["from"] == "s1"

    bad = _body()
    bad["edges"].append({"from": "s2", "to": "s1", "type": "after"})
    v = api.post("/api/v1/workflows/wf/validate", json=bad).json()
    assert v["ok"] is False and any("cycle" in e for e in v["errors"])
    assert api.put("/api/v1/workflows/wf", json=bad).status_code == 400

    dup = api.post("/api/v1/workflows/wf/duplicate", json={"new_name": "wf2"})
    assert dup.status_code == 201 and dup.json()["name"] == "wf2"
    assert api.get("/api/v1/workflows/wf2").json()["enabled"] is False

    assert api.post("/api/v1/workflows/wf2/enable").json()["enabled"] is True
    assert api.post("/api/v1/workflows/wf2/disable").json()["enabled"] is False

    run = api.post("/api/v1/workflows/wf/run")
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert run.json()["status"] == "running"
    again = api.post("/api/v1/workflows/wf/run")
    assert again.status_code == 400 and "in progress" in again.text
    runs = api.get("/api/v1/workflow-runs", params={"workflow": "wf"}).json()
    assert runs["total"] == 1 and runs["runs"][0]["run_id"] == run_id
    detail = api.get(f"/api/v1/workflow-runs/{run_id}").json()
    assert {s["step_id"] for s in detail["steps"]} == {"s1", "s2", "s3"}
    assert api.post(f"/api/v1/workflow-runs/{run_id}/cancel").status_code == 200

    draft = dict(_body(), name="wf_draft", enabled=True)
    d = api.post("/api/v1/workflow-drafts", json=draft)
    assert d.status_code == 201, d.text
    assert api.get("/api/v1/workflows/wf_draft").json()["enabled"] is False
    assert api.get("/api/v1/workflow-runs", params={"workflow": "wf_draft"}).json()["total"] == 0


@pytest.mark.unit
def test_api_delete_requires_approval(api: Any) -> None:
    api.post("/api/v1/workflows/wf", json=_body())
    r = api.delete("/api/v1/workflows/wf")
    assert r.status_code == 202 and r.json()["detail"]["status"] == "awaiting_approval"
    approval = r.json()["detail"]["approval_id"]
    from ovid2.web.models import ApprovalDecision
    from ovid2.web.services import approvals_svc

    approvals_svc.decide(approval, True, ApprovalDecision(decided_by="admin"))
    assert api.delete("/api/v1/workflows/wf").status_code == 200
    assert api.get("/api/v1/workflows/wf").status_code == 404


# --------------------------------------------------------------------------- HTML


@pytest.mark.unit
def test_html_pages_render_and_nav_entry(api: Any) -> None:
    empty = api.get("/workflows")
    assert empty.status_code == 200 and "No workflows yet" in empty.text
    assert 'href="/workflows"' in empty.text and "nav-item active" in empty.text
    api.post("/api/v1/workflows/wf", json=_body())
    page = api.get("/workflows")
    assert page.status_code == 200
    assert "data-wf-mini=" in page.text and "/workflows/wf/duplicate" in page.text
    editor = api.get("/workflows/wf")
    assert editor.status_code == 200
    assert 'id="wf-catalog"' in editor.text and "workflow_canvas.js" in editor.text
    assert '"p_ab"' in editor.text
    # Branching: the condition catalogue, link outcome select, join select, probes.
    assert '"ops_by_type"' in editor.text and 'id="ed-on"' in editor.text
    assert 'id="st-join"' in editor.text and "data-probe-add" in editor.text
    cat = api.get("/api/v1/workflow-conditions").json()
    assert "health.blockers" in {f["name"] for f in cat["fields"]}
    assert cat["on"] == ["success", "failure", "always", "otherwise"]
    assert api.get("/workflows/new").status_code == 200
    assert api.get("/workflows/nope").status_code == 404
    run_id = api.post("/api/v1/workflows/wf/run").json()["run_id"]
    view = api.get(f"/workflows/wf/runs/{run_id}")
    assert view.status_code == 200 and "workflow_run.js" in view.text
    # Form fallbacks.
    dup = api.post("/workflows/wf/duplicate", data={"new_name": "wf9"}, follow_redirects=False)
    assert dup.status_code == 303 and dup.headers["location"] == "/workflows/wf9"
    tog = api.post("/workflows/wf9/toggle", data={"enabled": "1"}, follow_redirects=False)
    assert tog.status_code == 303
    gone = api.post("/workflows/wf9/delete", follow_redirects=False)
    assert gone.status_code == 303 and workflows_svc.get_workflow("wf9") is None


@pytest.mark.unit
def test_concurrent_advance_queues_each_step_once(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The web process and the worker both advance runs; a step starts once.

    Reproduces the interleaving seen live: process B reads the step rows
    (``waiting``) while process A is between reading and queueing the job.
    """
    workflows_svc.write_workflow("wf", _chain(), create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    stale: list[dict[str, Any]] = []
    real_rows = workflow_runner_svc._step_rows

    def snapshot_rows(run_id: str) -> dict[str, dict[str, Any]]:
        rows = real_rows(run_id)
        if not stale:
            stale.append({k: dict(v) for k, v in rows.items()})
        return rows

    monkeypatch.setattr(workflow_runner_svc, "_step_rows", snapshot_rows)
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")  # process A queues s1
    monkeypatch.setattr(
        workflow_runner_svc, "_step_rows", lambda rid: {k: dict(v) for k, v in stale[0].items()}
    )
    run = workflow_runner_svc._run_row(run_id)
    assert run is not None
    workflow_runner_svc._advance_running(run)  # process B with the stale 'waiting' view
    monkeypatch.setattr(workflow_runner_svc, "_step_rows", real_rows)
    s1_jobs = [j for j in jobs_svc.list_jobs(limit=50).jobs if j.workflow_step_id == "s1"]
    assert len(s1_jobs) == 1, [j.job_id for j in s1_jobs]


# --------------------------------------------------------------------------- branching


def _branchy(**step_over: Any) -> WorkflowWrite:
    """s1 → s2 (blockers = 0) / s3 (otherwise); s2, s3 → s4 (join any)."""
    return WorkflowWrite(
        steps=[
            WorkflowStep(id="s1", pipeline="p_ab", **step_over),
            WorkflowStep(id="s2", pipeline="p_bc"),
            WorkflowStep(
                id="s3", pipeline="p_d", overrides=WorkflowStepOverrides(input_address="addr_b")
            ),
            WorkflowStep(
                id="s4",
                pipeline="p_bc",
                join="any",
                overrides=WorkflowStepOverrides(output_address="addr_e"),
            ),
        ],
        edges=[
            WorkflowEdge(
                source="s1",
                target="s2",
                type="data",
                when={"field": "health.blockers", "op": "eq", "value": 0},
            ),
            WorkflowEdge(source="s1", target="s3", type="after", on="otherwise"),
            WorkflowEdge(source="s2", target="s4", type="data"),
            WorkflowEdge(source="s3", target="s4", type="data"),
        ],
    )


@pytest.fixture()
def addr_e(env: dict[str, Path]) -> Path:
    (env["root"] / "e").mkdir()
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "addr_e.json").write_text(
        json.dumps({"connection": "store", "location": {"path": "e"}}), encoding="utf-8"
    )
    return env["root"] / "e"


def _health(monkeypatch: pytest.MonkeyPatch, blockers: int) -> None:
    from ovid2.web.services import health_report_svc

    monkeypatch.setattr(
        health_report_svc,
        "get_report",
        lambda iid: {
            "summary": {
                "checks_total": 3,
                "checks_passed": 3 - blockers,
                "checks_exception": blockers,
                "checks_error": 0,
                "blockers": blockers,
                "failed_by_severity": {"blocker": blockers},
            },
            "checks": [],
        },
    )


def _run(env: dict[str, Path], wf_def: WorkflowWrite, **sim: Any) -> Any:
    v = workflows_svc.validate(wf_def)
    assert v.ok, v.errors
    workflows_svc.write_workflow("wf", wf_def, create_only=True)
    wf = workflows_svc.get_workflow("wf")
    assert wf is not None
    run_id = workflow_runner_svc.start_run(wf, trigger="manual")
    worker = SimWorker(env["root"], **sim)
    worker.drain()
    run = workflow_runner_svc.get_run(run_id)
    assert run is not None
    return run, worker


@pytest.mark.unit
@pytest.mark.parametrize(("blockers", "taken", "not_taken"), [(0, "s2", "s3"), (2, "s3", "s2")])
def test_condition_routes_one_branch_and_join_any_runs(
    env: dict[str, Path],
    addr_e: Path,
    monkeypatch: pytest.MonkeyPatch,
    blockers: int,
    taken: str,
    not_taken: str,
) -> None:
    _health(monkeypatch, blockers)
    run, worker = _run(env, _branchy())
    steps = {s.step_id: s for s in run.steps}
    assert run.status == "succeeded", run.model_dump_json(indent=1)
    assert steps[taken].status == "done"
    assert steps[not_taken].status == "skipped" and steps[not_taken].reason
    assert steps["s4"].status == "done"
    # join any: s4 reads the output of whichever branch ran.
    assert steps["s4"].input_address == steps[taken].output_address
    assert run.skipped == 1
    decided = {(e.source, e.target): e for e in run.edges}
    assert decided[("s1", taken)].taken and not decided[("s1", not_taken)].taken
    assert "health.blockers" in decided[("s1", "s2")].reason
    assert [sid for sid, _ in worker.ran if sid] == ["s1", taken, "s4"]


@pytest.mark.unit
def test_join_all_skips_when_one_branch_not_taken(
    env: dict[str, Path], addr_e: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _health(monkeypatch, 0)
    wf_def = _branchy()
    wf_def.steps[3].join = "all"
    wf_def.edges[3].type = "after"  # one data input under join all
    run, _ = _run(env, wf_def)
    steps = {s.step_id: s for s in run.steps}
    assert run.status == "succeeded"
    assert steps["s3"].status == "skipped"
    assert steps["s4"].status == "skipped" and "s3" in (steps["s4"].reason or "")


@pytest.mark.unit
def test_fan_in_all_waits_for_every_parent(env: dict[str, Path], addr_e: Path) -> None:
    """Two independent roots → one step that starts only after both finish."""
    wf_def = WorkflowWrite(
        steps=[
            WorkflowStep(id="a", pipeline="p_ab"),
            WorkflowStep(id="b", pipeline="p_d"),
            WorkflowStep(
                id="c", pipeline="p_bc", overrides=WorkflowStepOverrides(output_address="addr_e")
            ),
        ],
        edges=[
            WorkflowEdge(source="a", target="c", type="data"),
            WorkflowEdge(source="b", target="c", type="after"),
        ],
    )
    run, worker = _run(env, wf_def)
    assert run.status == "succeeded"
    order = [sid for sid, _ in worker.ran if sid]
    assert order.index("c") > order.index("a") and order.index("c") > order.index("b")


@pytest.mark.unit
def test_failure_path_is_handled_and_rolled_back(env: dict[str, Path], addr_e: Path) -> None:
    root = env["root"]
    wf_def = WorkflowWrite(
        steps=[
            WorkflowStep(id="s1", pipeline="p_ab"),
            WorkflowStep(id="ok", pipeline="p_bc"),
            WorkflowStep(id="recover", pipeline="p_d"),
        ],
        edges=[
            WorkflowEdge(source="s1", target="ok", type="data"),
            WorkflowEdge(source="s1", target="recover", type="after", on="failure"),
        ],
    )
    run, _ = _run(env, wf_def, fail_steps={"s1"})
    steps = {s.step_id: s for s in run.steps}
    assert run.status == "succeeded", run.model_dump_json(indent=1)
    assert run.handled_failures == 1
    assert steps["s1"].status == "rolled_back" and steps["s1"].outcome == "failed"
    assert "recover" in (steps["s1"].reason or "")
    assert steps["ok"].status == "skipped"
    assert steps["recover"].status == "done"
    # s1's partial output was undone.
    assert list((root / "b").iterdir()) == []


@pytest.mark.unit
def test_unhandled_failure_still_rolls_back_whole_run(env: dict[str, Path], addr_e: Path) -> None:
    wf_def = WorkflowWrite(
        steps=[WorkflowStep(id="s1", pipeline="p_ab"), WorkflowStep(id="s2", pipeline="p_bc")],
        edges=[WorkflowEdge(source="s1", target="s2", type="data", on="always")],
    )
    run, _ = _run(env, wf_def, fail_steps={"s2"})
    assert run.status == "rolled_back"


@pytest.mark.unit
def test_data_probe_job_feeds_condition(
    env: dict[str, Path], addr_e: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe job runs the real tool (DuckDB) on the step's output; the number routes."""
    from ovid2.web.models import WorkflowProbe

    real_run = SimWorker._run
    probe_jobs: list[Any] = []

    def _run_job(self: SimWorker, job: Any) -> None:
        if job.extra_args.get("kind") != "workflow_probe":
            real_run(self, job)
            out = self.root / job.output_address.removeprefix("addr_")
            (out / "person.csv").write_text("id,sex\n1,F\n2,M\n3,F\n", encoding="utf-8")
            return
        from ovid2.web.worker import Worker, _last_json_line

        argv = Worker(ovid2_bin=["ovid2"])._build_argv(job)
        assert argv[:3] == ["ovid2", "tools", "workflow-probe"]
        from ovid2.tools.workflow_probe import run_probes

        result = run_probes(job.output_address, job.extra_args["probes"])
        log = jobs_svc.worker_log_path(job.job_id)
        log.write_text("[ovid2] probe\n" + json.dumps(result) + "\n", encoding="utf-8")
        jobs_svc.record_result(job.job_id, _last_json_line(log))
        jobs_svc.mark_terminal(
            job.job_id, status=jobs_svc.STATUS_DONE if result["ok"] else jobs_svc.STATUS_FAILED
        )
        probe_jobs.append(job)

    monkeypatch.setattr(SimWorker, "_run", _run_job)
    wf_def = _branchy(probes=[WorkflowProbe(name="female", table="person", where="sex = 'F'")])
    wf_def.edges[0].when = {"field": "query.female", "op": "gte", "value": 2}
    run, _ = _run(env, wf_def)
    steps = {s.step_id: s for s in run.steps}
    assert run.status == "succeeded", run.model_dump_json(indent=1)
    assert len(probe_jobs) == 1
    assert steps["s1"].facts and steps["s1"].facts["query"] == {"female": 2}
    assert steps["s2"].status == "done" and steps["s3"].status == "skipped"


@pytest.mark.unit
def test_branch_validation(env: dict[str, Path], addr_e: Path) -> None:
    wf_def = _branchy()
    wf_def.edges[0].when = {"field": "query.nope", "op": "gt", "value": 1}
    assert any("no data probe" in e for e in workflows_svc.validate(wf_def).errors)

    wf_def = _branchy()
    wf_def.edges[1].when = {"field": "status", "op": "eq", "value": "succeeded"}
    assert any("otherwise" in e for e in workflows_svc.validate(wf_def).errors)

    wf_def = _branchy()
    wf_def.steps[3].join = "all"
    assert any("join to 'any'" in e for e in workflows_svc.validate(wf_def).errors)

    wf_def = _branchy()
    wf_def.edges[0].when = {"field": "duration_s", "op": "starts_with", "value": "1"}
    assert any("does not apply" in e for e in workflows_svc.validate(wf_def).errors)

    from ovid2.web.models import WorkflowProbe

    wf_def = _branchy(probes=[WorkflowProbe(name="x", table="person", where="(select 1) = 1")])
    assert any("filter rejected" in e for e in workflows_svc.validate(wf_def).errors)

    # JSON round trip through the on-disk schema.
    good = _branchy(probes=[WorkflowProbe(name="female", table="person", where="sex = 'F'")])
    good.edges[0].when = {
        "all": [
            {"field": "query.female", "op": "gt", "value": 0},
            {"not": {"field": "batch_id", "op": "ends_with", "value": "_test"}},
        ]
    }
    workflows_svc.write_workflow("wfb", good, create_only=True)
    back = workflows_svc.get_workflow("wfb")
    assert back is not None and back.edges[0].when == good.edges[0].when
    assert back.edges[1].on == "otherwise" and back.steps[3].join == "any"
    assert back.steps[0].probes[0].where == "sex = 'F'"


@pytest.mark.unit
def test_worker_argv_and_result_for_probe_job(env: dict[str, Path]) -> None:
    from ovid2.web.worker import Worker, _last_json_line

    resp = jobs_svc.submit_workflow_probe(
        address="addr_b",
        probes=[{"name": "n", "table": "person"}],
        label="workflow:wf:probe",
        batch_id="b",
        workflow_run_id="r1",
        workflow_step_id="s1",
    )
    job = jobs_svc.get_job(resp.job_id)
    assert job is not None
    argv = Worker(ovid2_bin=["ovid2"])._build_argv(job)
    assert argv == [
        "ovid2",
        "tools",
        "workflow-probe",
        "--address",
        "addr_b",
        "--spec",
        '[{"name":"n","table":"person"}]',
        "--engine",
        "duckdb",
    ]
    log = jobs_svc.worker_log_path(resp.job_id)
    log.write_text('noise\n{"ok": true, "results": {"n": 3}}\n# trailer\n', encoding="utf-8")
    jobs_svc.record_result(resp.job_id, _last_json_line(log))
    assert jobs_svc.get_job(resp.job_id).result == {"ok": True, "results": {"n": 3}}  # type: ignore[union-attr]


@pytest.mark.unit
def test_agent_filtered_probe_needs_approval(
    api: Any, addr_e: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent adding a probe with a row filter gets 202 until a human approves."""
    from ovid2.web.auth import deps
    from ovid2.web.auth.roles import Role
    from ovid2.web.models import ApprovalDecision, WorkflowProbe
    from ovid2.web.services import approvals_svc

    wf_def = _branchy(probes=[WorkflowProbe(name="female", table="person", where="sex = 'F'")])
    body = json.loads(wf_def.model_dump_json(by_alias=True))
    # A human is not gated.
    assert api.post("/api/v1/workflows/human", json=body).status_code == 201
    monkeypatch.setattr(
        deps,
        "current_user",
        lambda _req: deps.AuthContext(user_id="mcp:service", role=Role.SYSTEM_ADMIN),
    )
    # Probes without a filter are not gated for agents either.
    plain = json.loads(
        _branchy(probes=[WorkflowProbe(name="n", table="person")]).model_dump_json(by_alias=True)
    )
    assert api.post("/api/v1/workflows/agent_plain", json=plain).status_code == 201
    r = api.post("/api/v1/workflows/agent", json=body)
    assert r.status_code == 202 and r.json()["detail"]["action"] == "workflow.data_probe"
    approvals_svc.decide(
        r.json()["detail"]["approval_id"], True, ApprovalDecision(decided_by="human")
    )
    assert api.post("/api/v1/workflows/agent", json=body).status_code == 201
