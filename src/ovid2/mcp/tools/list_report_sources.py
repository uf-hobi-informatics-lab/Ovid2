"""``list_report_sources`` — enumerate Report Studio sources."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListReportSourcesArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    kind: str | None = Field(None, description="health | analysis | run_summary | health_trend.")
    pipeline: str | None = Field(None, description="Pipeline name filter.")
    q: str | None = Field(None, description="Substring of title / batch / id.")
    limit: int = Field(50, ge=1, le=500)


TOOL_NAME = "list_report_sources"
TOOL_DESCRIPTION = (
    "List Report Studio sources: durable, aggregated datasets published "
    "by finished runs (health checks, analysis profiles, run summaries, "
    "health trends). No rows are returned."
)
ARGS_MODEL = ListReportSourcesArgs


async def run(client: WebApiClient, args: ListReportSourcesArgs) -> Any:
    """Call ``GET /api/v1/report-sources``."""
    return await client.get(
        "/api/v1/report-sources", kind=args.kind, pipeline=args.pipeline, q=args.q, limit=args.limit
    )
