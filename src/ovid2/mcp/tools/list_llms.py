"""``list_llms`` — enumerate LLM connections (general + System 1)."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_llms"
TOOL_DESCRIPTION = (
    "Enumerate the LLM connections configured in Ovid2 (role general / "
    "system1, provider, model, endpoint). API keys are never returned."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/llms``."""
    return await client.get("/api/v1/llms")
