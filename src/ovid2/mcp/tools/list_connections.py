"""``list_connections`` — enumerate every connection config."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_connections"
TOOL_DESCRIPTION = (
    "Enumerate every connection config discoverable via the XDG "
    "search path. Returns a list of ConnectionSummary objects "
    "(name, type, path, secret_ref). Credential values are never "
    "returned."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/connections``."""
    return await client.get("/api/v1/connections")
