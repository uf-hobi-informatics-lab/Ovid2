"""``delete_engine`` — remove an engine JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteEngineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the engine.")


TOOL_NAME = "delete_engine"
TOOL_DESCRIPTION = (
    "Remove an engine JSON. Refuses (HTTP 400) when the engine is "
    "still referenced by any pipeline or by settings.defaults.engine. "
    "Does not delete the referenced secret file — call delete_secret "
    "separately if desired."
)
ARGS_MODEL = DeleteEngineArgs


async def run(client: WebApiClient, args: DeleteEngineArgs) -> Any:
    """Call ``DELETE /api/v1/engines/{name}``."""
    return await client.delete(f"/api/v1/engines/{args.name}")
