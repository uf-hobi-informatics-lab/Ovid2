"""``get_secret`` — describe a secret file (keys only, never values)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetSecretArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the secret JSON blob.")


TOOL_NAME = "get_secret"
TOOL_DESCRIPTION = (
    "Return the top-level key names of a secret file — values are "
    "never returned by any code path. Use write_secret to update "
    "values."
)
ARGS_MODEL = GetSecretArgs


async def run(client: WebApiClient, args: GetSecretArgs) -> Any:
    """Call ``GET /api/v1/secrets/{name}``."""
    return await client.get(f"/api/v1/secrets/{args.name}")
