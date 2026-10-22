"""``list_approvals`` — pending destructive-op approval requests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListApprovalsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    status: str | None = Field(
        default=None,
        description=(
            "Optional status filter: pending / approved / denied / consumed."
        ),
    )


TOOL_NAME = "list_approvals"
TOOL_DESCRIPTION = (
    "List human-approval requests for destructive operations (deleting a "
    "secret, connection, engine, pipeline, or address). An agent cannot "
    "approve its own requests — approval is a human-only action performed "
    "in the web control panel. Use this to check whether a gated "
    "operation you attempted is still awaiting approval."
)
ARGS_MODEL = ListApprovalsArgs


async def run(client: WebApiClient, args: ListApprovalsArgs) -> Any:
    """Call ``GET /api/v1/approvals``."""
    return await client.get("/api/v1/approvals", status=args.status)
