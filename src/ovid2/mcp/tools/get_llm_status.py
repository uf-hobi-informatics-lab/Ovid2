"""``get_llm_status`` — assistant routing status."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "get_llm_status"
TOOL_DESCRIPTION = (
    "Return whether the Ovid2 assistant is enabled and which LLM "
    "connections it routes to (System 1 first, general fallback)."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/llm/status``."""
    return await client.get("/api/v1/llm/status")
