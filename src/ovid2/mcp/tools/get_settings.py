"""``get_settings`` — return the global runtime settings."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "get_settings"
TOOL_DESCRIPTION = (
    "Return the merged global runtime settings — defaults, worker, "
    "UI polling, notifications, retention. Passwords are never "
    "returned in this payload."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/settings``."""
    return await client.get("/api/v1/settings")
