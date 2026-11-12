"""``get_report_source`` — describe one report source (no rows)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetReportSourceArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    source_id: str = Field(..., description="Report source id from list_report_sources.")


TOOL_NAME = "get_report_source"
TOOL_DESCRIPTION = (
    "Describe one report source: its datasets and columns (type, "
    "sensitivity, distinct count, ranges / examples). Raw-value columns "
    "are masked for agents; rows are never returned."
)
ARGS_MODEL = GetReportSourceArgs


async def run(client: WebApiClient, args: GetReportSourceArgs) -> Any:
    """Call ``GET /api/v1/report-sources/{source_id}``."""
    return await client.get(f"/api/v1/report-sources/{quote(args.source_id, safe='')}")
