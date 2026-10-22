"""``list_pipelines`` — enumerate every pipeline package."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_pipelines"
TOOL_DESCRIPTION = (
    "Enumerate every discoverable ovid2_pipelines.* package. Returns a "
    "list of PipelineSummary objects (name, mappers_type, default_db, "
    "path, formatter_count, mapper_count)."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/pipelines``."""
    return await client.get("/api/v1/pipelines")
