"""``list_workspaces`` — the workspaces this agent may act in."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ovid2.mcp.client import WebApiClient


class ListWorkspacesArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME` (none)."""


TOOL_NAME = "list_workspaces"
TOOL_DESCRIPTION = (
    "List the workspace this agent is acting in. A workspace is the shared "
    "space whose pipelines, workflows, jobs, reports and configuration every "
    "other tool sees; nothing outside it is visible. The workspace is fixed "
    "by the operator (OVID2_MCP_WORKSPACE + the service token's bound "
    "workspaces) and cannot be changed from a tool call."
)
ARGS_MODEL = ListWorkspacesArgs


async def run(client: WebApiClient, args: ListWorkspacesArgs) -> Any:
    """Call ``GET /api/v1/me/workspaces``."""
    return await client.get("/api/v1/me/workspaces")
