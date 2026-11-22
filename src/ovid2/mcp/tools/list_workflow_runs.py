"""``list_workflow_runs`` — recent workflow runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListWorkflowRunsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    workflow: str | None = Field(None, description="Only runs of this workflow.")
    limit: int = Field(20, ge=1, le=500)
    offset: int = Field(0, ge=0)


TOOL_NAME = "list_workflow_runs"
TOOL_DESCRIPTION = "Return workflow runs, newest first (optionally for one workflow)."
ARGS_MODEL = ListWorkflowRunsArgs


async def run(client: WebApiClient, args: ListWorkflowRunsArgs) -> Any:
    """Call ``GET /api/v1/workflow-runs``."""
    return await client.get(
        "/api/v1/workflow-runs", workflow=args.workflow, limit=args.limit, offset=args.offset
    )
