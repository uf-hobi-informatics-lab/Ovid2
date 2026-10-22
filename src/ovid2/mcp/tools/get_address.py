"""``get_address`` — fetch one address config (secrets masked)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetAddressArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the address config to fetch.")


TOOL_NAME = "get_address"
TOOL_DESCRIPTION = (
    "Fetch one address config by name. Sensitive src_args (password, "
    "token) are masked as '***'. To update the underlying value, call "
    "update_address with the new value in place of the sentinel."
)
ARGS_MODEL = GetAddressArgs


async def run(client: WebApiClient, args: GetAddressArgs) -> Any:
    """Call ``GET /api/v1/addresses/{name}``."""
    return await client.get(f"/api/v1/addresses/{args.name}")
