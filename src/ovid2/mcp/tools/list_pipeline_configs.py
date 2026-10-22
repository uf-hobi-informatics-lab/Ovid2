"""``list_pipeline_configs`` — enumerate every pipeline_config."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_pipeline_configs"
TOOL_DESCRIPTION = (
    "Enumerate every pipeline_config JSON on the XDG search path. Returns a "
    "list of PipelineConfigSummary objects (name, pipeline, addresses, trigger "
    "type, enabled, next_fire_at)."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/pipelines``."""
    return await client.get("/api/v1/pipelines")
