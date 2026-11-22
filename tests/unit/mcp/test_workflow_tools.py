"""Unit tests — workflow MCP tools against the in-process API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

WORKFLOW_TOOLS = {
    "list_workflows",
    "get_workflow",
    "create_workflow",
    "update_workflow",
    "duplicate_workflow",
    "run_workflow",
    "get_workflow_run",
    "list_workflow_runs",
    "cancel_workflow_run",
}


def test_workflow_tools_discovered() -> None:
    from ovid2.mcp import tools as tools_pkg

    assert {s.name for s in tools_pkg.discover_tools()} >= WORKFLOW_TOOLS


def test_workflow_tools_round_trip(
    mcp_client: Any, isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    from ovid2.mcp.tools import (
        create_workflow,
        duplicate_workflow,
        get_workflow,
        get_workflow_run,
        list_workflow_runs,
        list_workflows,
        run_workflow,
        update_workflow,
    )
    from ovid2.web.models import PipelineTrigger, PipelineWrite
    from ovid2.web.services import jobs_svc, pipelines_svc

    monkeypatch.setenv("OVID2_WORKFLOWS_DIR", str(tmp_path / "wf"))
    monkeypatch.setenv("OVID2_PIPELINES_DIR", str(tmp_path / "pl"))
    monkeypatch.setattr(jobs_svc, "check_overwrite", lambda *a, **k: [])
    monkeypatch.setattr(jobs_svc, "_expand_all_tables", lambda request: None)
    from ovid2.web.services import output_probe_svc

    monkeypatch.setattr(
        output_probe_svc,
        "existing_output_tables",
        lambda a: output_probe_svc.OutputProbe(known=True, tables=[]),
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    for name in ("a", "b", "c"):
        (configs / f"x_{name}.json").write_text(
            json.dumps({"src_name": "disc", "src_args": {"path": str(tmp_path / name)}}),
            encoding="utf-8",
        )
    for name, inp, out in (("one", "x_a", "x_b"), ("two", "", "x_c")):
        pipelines_svc.write_pipeline(
            name,
            PipelineWrite(
                kind="copy",
                pipeline=name,
                input_address=inp,
                output_address=out,
                tables=["t"],
                engine="duckdb",
                trigger=PipelineTrigger(type="manual"),
            ),
            create_only=True,
        )

    body = create_workflow.CreateWorkflowArgs.model_validate(
        {
            "name": "mcp_wf",
            "steps": [
                {"id": "s1", "pipeline": "one", "rollback": False},
                {"id": "s2", "pipeline": "two", "rollback": False},
            ],
            "edges": [{"from": "s1", "to": "s2", "type": "data"}],
        }
    )

    async def _call() -> None:
        res = await create_workflow.run(mcp_client, body)
        assert res["action"] == "created"
        listing = await list_workflows.run(mcp_client, list_workflows.ListWorkflowsArgs())
        assert [w["name"] for w in listing] == ["mcp_wf"]
        got = await get_workflow.run(mcp_client, get_workflow.GetWorkflowArgs(name="mcp_wf"))
        assert [{k: e[k] for k in ("from", "to", "type")} for e in got["edges"]] == [
            {"from": "s1", "to": "s2", "type": "data"}
        ]
        assert got["edges"][0]["on"] == "success" and got["edges"][0]["when"] is None
        upd = update_workflow.UpdateWorkflowArgs.model_validate(
            {**body.model_dump(by_alias=True), "description": "nightly"}
        )
        assert (await update_workflow.run(mcp_client, upd))["action"] == "updated"
        dup = await duplicate_workflow.run(
            mcp_client, duplicate_workflow.DuplicateWorkflowArgs(name="mcp_wf")
        )
        assert dup["name"] == "mcp_wf_copy"
        run = await run_workflow.run(mcp_client, run_workflow.RunWorkflowArgs(name="mcp_wf"))
        assert run["status"] == "running"
        again = await get_workflow_run.run(
            mcp_client, get_workflow_run.GetWorkflowRunArgs(run_id=run["run_id"])
        )
        assert {s["step_id"] for s in again["steps"]} == {"s1", "s2"}
        runs = await list_workflow_runs.run(
            mcp_client, list_workflow_runs.ListWorkflowRunsArgs(workflow="mcp_wf")
        )
        assert runs["total"] == 1

    anyio.run(_call)
