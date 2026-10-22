"""``install_status`` — aggregate install-wizard state."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "install_status"
TOOL_DESCRIPTION = (
    "Return the wizard's step-by-step status, derived live from disk "
    "on every call. Steps: bootstrap, snowflake, addresses, image, "
    "service, pipeline, done."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/install/status``."""
    return await client.get("/api/v1/install/status")
