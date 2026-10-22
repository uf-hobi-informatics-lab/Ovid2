"""``get_connection`` — fetch one connection with masked secrets."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetConnectionArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the connection.")


TOOL_NAME = "get_connection"
TOOL_DESCRIPTION = (
    "Return the parsed contents of one connection config. Every "
    "value in the ``secrets`` dict is the sentinel ``'***'`` — the "
    "server never exposes plaintext credentials via the API."
)
ARGS_MODEL = GetConnectionArgs


async def run(client: WebApiClient, args: GetConnectionArgs) -> Any:
    """Call ``GET /api/v1/connections/{name}``."""
    return await client.get(f"/api/v1/connections/{args.name}")
