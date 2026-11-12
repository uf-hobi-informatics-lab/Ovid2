"""``list_reports`` — reports visible to the caller."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListReportsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    scope: str = Field("all", pattern="^(all|mine|shared|favorites)$")
    q: str | None = Field(None, description="Search title / description / tags.")
    source_id: str | None = Field(None, description="Only reports on this source.")


TOOL_NAME = "list_reports"
TOOL_DESCRIPTION = (
    "List the reports visible to the service principal (title, folder, "
    "tags, source, version, permission)."
)
ARGS_MODEL = ListReportsArgs


async def run(client: WebApiClient, args: ListReportsArgs) -> Any:
    """Call ``GET /api/v1/reports``."""
    return await client.get("/api/v1/reports", scope=args.scope, q=args.q, source_id=args.source_id)
