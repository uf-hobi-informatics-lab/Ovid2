"""``get_report_catalogue`` — chart kinds, themes, edit ops, formats."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "get_report_catalogue"
TOOL_DESCRIPTION = (
    "Return the Report Studio library: chart kinds with their params "
    "JSON Schemas, report themes, layout edit ops and export formats."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/report-catalogue``."""
    return await client.get("/api/v1/report-catalogue")
