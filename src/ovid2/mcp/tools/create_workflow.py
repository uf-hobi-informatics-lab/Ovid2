"""``create_workflow`` — create a workflow (chain of saved pipelines)."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient
from ovid2.mcp.workflow_args import WorkflowBodyArgs


class CreateWorkflowArgs(WorkflowBodyArgs):
    """Arguments for :data:`TOOL_NAME`."""


TOOL_NAME = "create_workflow"
TOOL_DESCRIPTION = (
    "Create a workflow: steps that each run a saved pipeline config, linked by "
    "'data' (next step reads the previous output) or 'after' (order only) edges. "
    "The workflow owns its trigger (manual / cron / interval / watch). On failure "
    "finished steps are rolled back. 400 when the name exists or the DAG is invalid."
)
ARGS_MODEL = CreateWorkflowArgs


async def run(client: WebApiClient, args: CreateWorkflowArgs) -> Any:
    """Call ``POST /api/v1/workflows/{name}``."""
    return await client.post(f"/api/v1/workflows/{args.name}", json_body=args.body())
