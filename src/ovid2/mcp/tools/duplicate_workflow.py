"""``duplicate_workflow`` — copy a workflow under a new name."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DuplicateWorkflowArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Workflow to copy.")
    new_name: str | None = Field(None, description="Name of the copy (default <name>_copy).")


TOOL_NAME = "duplicate_workflow"
TOOL_DESCRIPTION = "Copy a workflow under a new name; the copy starts disabled."
ARGS_MODEL = DuplicateWorkflowArgs


async def run(client: WebApiClient, args: DuplicateWorkflowArgs) -> Any:
    """Call ``POST /api/v1/workflows/{name}/duplicate``."""
    return await client.post(
        f"/api/v1/workflows/{args.name}/duplicate", json_body={"new_name": args.new_name}
    )
