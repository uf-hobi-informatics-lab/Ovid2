"""``create_connection`` — create a new connection JSON config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CreateConnectionArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem for the new connection.")
    type: str = Field(..., description="Connector type — see list_connection_types.")
    args: dict[str, Any] = Field(default_factory=dict, description="Non-secret arguments.")
    secrets: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Secret material. Written to secrets.d/*.json with mode 0o600; "
            "the connection JSON only holds a secret_ref."
        ),
    )
    secret_ref: str | None = Field(
        None,
        description="Override the auto-generated secret file stem (default: conn_<name>).",
    )


TOOL_NAME = "create_connection"
TOOL_DESCRIPTION = (
    "Create a new connection JSON config. Fails with HTTP 400 when a "
    "connection of the same name already exists. Use update_connection "
    "for idempotent create-or-update."
)
ARGS_MODEL = CreateConnectionArgs


async def run(client: WebApiClient, args: CreateConnectionArgs) -> Any:
    """Call ``POST /api/v1/connections/{name}``."""
    body: dict[str, Any] = {
        "type": args.type,
        "args": args.args,
        "secrets": args.secrets,
    }
    if args.secret_ref:
        body["secret_ref"] = args.secret_ref
    return await client.post(f"/api/v1/connections/{args.name}", json_body=body)
