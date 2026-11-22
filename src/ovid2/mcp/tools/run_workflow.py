"""``run_workflow`` — start a workflow run now."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RunWorkflowArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Workflow name.")
    input_address: str | None = Field(
        None, description="Optional address the root step reads for this run."
    )


TOOL_NAME = "run_workflow"
TOOL_DESCRIPTION = (
    "Start a workflow run regardless of its trigger. Returns the run (poll it with "
    "get_workflow_run). A failed step stops the run and rolls back finished steps."
)
ARGS_MODEL = RunWorkflowArgs


async def run(client: WebApiClient, args: RunWorkflowArgs) -> Any:
    """Call ``POST /api/v1/workflows/{name}/run``."""
    body = {"input_address": args.input_address} if args.input_address else {}
    return await client.post(f"/api/v1/workflows/{args.name}/run", json_body=body)
