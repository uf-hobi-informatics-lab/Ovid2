"""``get_workflow_run`` — one workflow run with per-step state."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetWorkflowRunArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    run_id: str = Field(..., description="Workflow run id.")


TOOL_NAME = "get_workflow_run"
TOOL_DESCRIPTION = (
    "Return one workflow run: status (running / succeeded / rolled_back / "
    "rollback_failed / cancelled) and each step's job, addresses, error and "
    "rollback action."
)
ARGS_MODEL = GetWorkflowRunArgs


async def run(client: WebApiClient, args: GetWorkflowRunArgs) -> Any:
    """Call ``GET /api/v1/workflow-runs/{run_id}``."""
    return await client.get(f"/api/v1/workflow-runs/{args.run_id}")
