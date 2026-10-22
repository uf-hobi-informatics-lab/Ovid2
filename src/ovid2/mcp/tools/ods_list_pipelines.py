"""``ods_list_pipelines`` — enumerate pipelines in the Ovid Data Space."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "ods_list_pipelines"
TOOL_DESCRIPTION = (
    "List every pipeline discoverable in the Ovid Data Space, together "
    "with the storage dimensions each pipeline is known in. Returns an "
    "OdsPipelineListing (pipelines: [{name, known_dimensions}])."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/dataspace``."""
    return await client.get("/api/v1/dataspace")
