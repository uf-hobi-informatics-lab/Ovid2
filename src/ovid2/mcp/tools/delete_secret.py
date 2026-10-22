"""``delete_secret`` — remove a secret file from secrets.d/."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteSecretArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the secret to delete.")


TOOL_NAME = "delete_secret"
TOOL_DESCRIPTION = "Remove a secret JSON file from the user-scoped secrets.d/ directory."
ARGS_MODEL = DeleteSecretArgs


async def run(client: WebApiClient, args: DeleteSecretArgs) -> Any:
    """Call ``DELETE /api/v1/secrets/{name}``."""
    return await client.delete(f"/api/v1/secrets/{args.name}")
