"""``delete_connection`` — remove a connection JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteConnectionArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the connection.")


TOOL_NAME = "delete_connection"
TOOL_DESCRIPTION = (
    "Remove a connection JSON. Does not delete the referenced secret "
    "file — call delete_secret separately if desired."
)
ARGS_MODEL = DeleteConnectionArgs


async def run(client: WebApiClient, args: DeleteConnectionArgs) -> Any:
    """Call ``DELETE /api/v1/connections/{name}``."""
    return await client.delete(f"/api/v1/connections/{args.name}")
