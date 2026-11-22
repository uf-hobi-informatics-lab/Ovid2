"""``list_workflows`` — every workflow (chain of saved pipelines)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ovid2.mcp.client import WebApiClient


class ListWorkflowsArgs(BaseModel):
    """No arguments."""


TOOL_NAME = "list_workflows"
TOOL_DESCRIPTION = (
    "List workflows — DAGs of saved pipeline configs — with trigger, enabled "
    "flag, step / link counts and last run status."
)
ARGS_MODEL = ListWorkflowsArgs


async def run(client: WebApiClient, args: ListWorkflowsArgs) -> Any:
    """Call ``GET /api/v1/workflows``."""
    del args
    return await client.get("/api/v1/workflows")
