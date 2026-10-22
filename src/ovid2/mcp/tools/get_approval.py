"""``get_approval`` — fetch one destructive-op approval request."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetApprovalArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    approval_id: str = Field(..., description="The approval request id.")


TOOL_NAME = "get_approval"
TOOL_DESCRIPTION = (
    "Fetch the status of a single human-approval request by id. Returns "
    "its action, target, status (pending / approved / denied / consumed), "
    "and any decision note. An agent cannot approve requests — approval "
    "is performed by a human in the web control panel."
)
ARGS_MODEL = GetApprovalArgs


async def run(client: WebApiClient, args: GetApprovalArgs) -> Any:
    """Call ``GET /api/v1/approvals/{approval_id}``."""
    return await client.get(f"/api/v1/approvals/{args.approval_id}")
