"""``write_secret`` — create-or-update a secret JSON blob (0o600)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class WriteSecretArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the secret JSON blob.")
    values: dict[str, Any] = Field(
        ...,
        description=(
            "Top-level JSON object to persist. Any key sent as the "
            "sentinel string '***' preserves the on-disk value."
        ),
    )


TOOL_NAME = "write_secret"
TOOL_DESCRIPTION = (
    "Idempotent create-or-update of a secret JSON blob under "
    "secrets.d/. The file is written with mode 0o600. Overwriting "
    "an existing file that is not already 0o600 is refused."
)
ARGS_MODEL = WriteSecretArgs


async def run(client: WebApiClient, args: WriteSecretArgs) -> Any:
    """Call ``PUT /api/v1/secrets/{name}``."""
    return await client.put(
        f"/api/v1/secrets/{args.name}",
        json_body={"values": args.values},
    )
