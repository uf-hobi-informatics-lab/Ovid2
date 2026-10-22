"""``list_runs`` — paginated run history."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListRunsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    limit: int = Field(50, ge=1, le=500, description="Page size.")
    offset: int = Field(0, ge=0, description="Skip this many rows.")
    status: str | None = Field(
        None, description="Exact-match status filter (NEW/PROCESSING/PROCESSED/ERROR)."
    )
    pipeline: str | None = Field(None, description="Pipeline name filter.")


TOOL_NAME = "list_runs"
TOOL_DESCRIPTION = (
    "Return a paginated slice of the run history, joining the "
    "scheduler batch_tracker with web_jobs where available."
)
ARGS_MODEL = ListRunsArgs


async def run(client: WebApiClient, args: ListRunsArgs) -> Any:
    """Call ``GET /api/v1/runs``."""
    return await client.get(
        "/api/v1/runs",
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        pipeline=args.pipeline,
    )
