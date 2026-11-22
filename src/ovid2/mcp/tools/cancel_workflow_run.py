"""``cancel_workflow_run`` — stop a running workflow (finished steps roll back)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CancelWorkflowRunArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    run_id: str = Field(..., description="Workflow run id.")


TOOL_NAME = "cancel_workflow_run"
TOOL_DESCRIPTION = (
    "Cancel a running workflow. Active step jobs are cancelled and every finished "
    "step is rolled back."
)
ARGS_MODEL = CancelWorkflowRunArgs


async def run(client: WebApiClient, args: CancelWorkflowRunArgs) -> Any:
    """Call ``POST /api/v1/workflow-runs/{run_id}/cancel``."""
    return await client.post(f"/api/v1/workflow-runs/{args.run_id}/cancel", json_body={})
