"""``delete_address`` — remove an address JSON config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteAddressArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the address config.")


TOOL_NAME = "delete_address"
TOOL_DESCRIPTION = (
    "Remove an address JSON config from the user-scoped configs "
    "directory. System-scoped configs (/etc/ovid2/configs/) are "
    "never touched."
)
ARGS_MODEL = DeleteAddressArgs


async def run(client: WebApiClient, args: DeleteAddressArgs) -> Any:
    """Call ``DELETE /api/v1/addresses/{name}``."""
    return await client.delete(f"/api/v1/addresses/{args.name}")
