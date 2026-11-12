"""``list_transform_library`` — the reusable transform chains (UDFs)."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_transform_library"
TOOL_DESCRIPTION = (
    "List reusable transform chains: the user library "
    "($XDG_CONFIG_HOME/ovid2/transforms, scope 'user') and the shipped chains "
    "(scope 'shipped', read-only). Each entry: name, scope, description, steps, ops."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/transform-library``."""
    return await client.get("/api/v1/transform-library")
