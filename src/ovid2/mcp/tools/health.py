"""``health`` — return the aggregate doctor report."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "health"
TOOL_DESCRIPTION = (
    "Return the aggregate ovid2 doctor report — Docker, JARs, Snowflake, "
    "and permission checks. Response shape matches HealthResponse in "
    "ovid2.web.models (worst severity + per-check breakdown)."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/health``."""
    return await client.get("/api/v1/health")
