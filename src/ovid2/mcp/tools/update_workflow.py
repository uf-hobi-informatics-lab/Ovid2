"""``update_workflow`` — create-or-replace a workflow definition."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient
from ovid2.mcp.workflow_args import WorkflowBodyArgs


class UpdateWorkflowArgs(WorkflowBodyArgs):
    """Arguments for :data:`TOOL_NAME`."""


TOOL_NAME = "update_workflow"
TOOL_DESCRIPTION = (
    "Replace a workflow definition (steps, links, trigger). Read it first with "
    "get_workflow and send the complete definition back."
)
ARGS_MODEL = UpdateWorkflowArgs


async def run(client: WebApiClient, args: UpdateWorkflowArgs) -> Any:
    """Call ``PUT /api/v1/workflows/{name}``."""
    return await client.put(f"/api/v1/workflows/{args.name}", json_body=args.body())
