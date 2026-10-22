"""``list_connection_types`` — return the connector field-spec catalogue."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_connection_types"
TOOL_DESCRIPTION = (
    "Return the registered connector types and their field specs. "
    "Consumers use this to build the dynamic new-connection form and "
    "to validate ``create_connection`` args client-side."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/connections/types``."""
    return await client.get("/api/v1/connections/types")
