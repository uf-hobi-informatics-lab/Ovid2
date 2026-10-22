"""``get_engine`` — fetch one engine with masked secrets."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetEngineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the engine.")


TOOL_NAME = "get_engine"
TOOL_DESCRIPTION = (
    "Return the parsed contents of one engine config. Every value in "
    "the ``secrets`` dict is the sentinel ``'***'`` — the server never "
    "exposes plaintext credentials via the API."
)
ARGS_MODEL = GetEngineArgs


async def run(client: WebApiClient, args: GetEngineArgs) -> Any:
    """Call ``GET /api/v1/engines/{name}``."""
    return await client.get(f"/api/v1/engines/{args.name}")
