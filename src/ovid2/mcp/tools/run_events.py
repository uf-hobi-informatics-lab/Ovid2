"""``run_events`` — cursor-paged JSONL events for one run."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RunEventsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    instance_id: str = Field(..., description="Run instance id (YYYYMMDD-HHMMSS-<6hex>).")
    after: int = Field(
        0,
        ge=0,
        description="Byte-offset cursor returned by the previous call (use 0 to start from the top).",
    )
    limit: int = Field(500, ge=1, le=5000, description="Maximum events to return.")


TOOL_NAME = "run_events"
TOOL_DESCRIPTION = (
    "Fetch a cursor-paged slice of the JSONL event log for one run. "
    "Pass the returned next_cursor as after= to continue tailing. "
    "The 'finished' flag flips to true when instance.finished is "
    "seen."
)
ARGS_MODEL = RunEventsArgs


async def run(client: WebApiClient, args: RunEventsArgs) -> Any:
    """Call ``GET /api/v1/runs/{instance_id}/events``."""
    return await client.get(
        f"/api/v1/runs/{args.instance_id}/events",
        after=args.after,
        limit=args.limit,
    )
