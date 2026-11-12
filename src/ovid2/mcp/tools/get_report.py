"""``get_report`` — one report with layout + outline."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetReportArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    report_id: str = Field(..., description="Report id (rpt_…).")


TOOL_NAME = "get_report"
TOOL_DESCRIPTION = (
    "Return one report: metadata, its full layout and a compact outline "
    "with the section / block ids edit ops refer to."
)
ARGS_MODEL = GetReportArgs


async def run(client: WebApiClient, args: GetReportArgs) -> Any:
    """Call ``GET /api/v1/reports/{report_id}``."""
    return await client.get(f"/api/v1/reports/{quote(args.report_id, safe='')}")
