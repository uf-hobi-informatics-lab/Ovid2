"""``list_addresses`` — enumerate every address config."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_addresses"
TOOL_DESCRIPTION = (
    "Enumerate every address config discoverable via the XDG search "
    "path. Returns a list of AddressSummary objects (name, src_name, "
    "path). Values in secrets are never returned by this tool."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/addresses``."""
    return await client.get("/api/v1/addresses")
