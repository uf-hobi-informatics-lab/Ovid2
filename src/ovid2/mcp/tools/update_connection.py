"""``update_connection`` — idempotent create-or-update of a connection JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class UpdateConnectionArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the connection.")
    type: str = Field(..., description="Connector type.")
    args: dict[str, Any] = Field(default_factory=dict, description="Non-secret arguments.")
    secrets: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Secret material. Any value of '***' is treated as 'keep the "
            "existing value on disk'."
        ),
    )
    secret_ref: str | None = Field(None, description="Override the secret filename stem.")


TOOL_NAME = "update_connection"
TOOL_DESCRIPTION = (
    "Create or update a connection JSON config. Secrets round-trip via "
    "the sentinel '***' so masked reads survive edits."
)
ARGS_MODEL = UpdateConnectionArgs


async def run(client: WebApiClient, args: UpdateConnectionArgs) -> Any:
    """Call ``PUT /api/v1/connections/{name}``."""
    body: dict[str, Any] = {
        "type": args.type,
        "args": args.args,
        "secrets": args.secrets,
    }
    if args.secret_ref:
        body["secret_ref"] = args.secret_ref
    return await client.put(f"/api/v1/connections/{args.name}", json_body=body)
