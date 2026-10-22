"""``list_jobs`` — paginated web_jobs listing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListJobsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    limit: int = Field(50, ge=1, le=500, description="Page size.")
    offset: int = Field(0, ge=0, description="Skip this many rows.")
    status: str | None = Field(None, description="Exact-match status filter.")
    pipeline: str | None = Field(None, description="Pipeline name filter.")


TOOL_NAME = "list_jobs"
TOOL_DESCRIPTION = (
    "Return a paginated slice of web_jobs — every ETL job submitted "
    "through the web tier, in reverse-chronological order."
)
ARGS_MODEL = ListJobsArgs


async def run(client: WebApiClient, args: ListJobsArgs) -> Any:
    """Call ``GET /api/v1/jobs``."""
    return await client.get(
        "/api/v1/jobs",
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        pipeline=args.pipeline,
    )
