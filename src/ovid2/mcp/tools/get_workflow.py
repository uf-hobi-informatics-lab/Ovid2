"""``get_workflow`` — one workflow definition + runtime state."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetWorkflowArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Workflow name.")


TOOL_NAME = "get_workflow"
TOOL_DESCRIPTION = (
    "Return one workflow: steps (pipeline config + overrides + rollback flag), "
    "links (from, to, type data|after), trigger, next fire and last run."
)
ARGS_MODEL = GetWorkflowArgs


async def run(client: WebApiClient, args: GetWorkflowArgs) -> Any:
    """Call ``GET /api/v1/workflows/{name}``."""
    return await client.get(f"/api/v1/workflows/{args.name}")
