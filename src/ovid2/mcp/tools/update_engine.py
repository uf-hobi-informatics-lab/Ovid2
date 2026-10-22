"""``update_engine`` — idempotent create-or-update of an engine JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class UpdateEngineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the engine.")
    type: str = Field(..., description="Engine type.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret compute knobs."
    )
    secrets: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Credential material. Any value of '***' is treated as 'keep "
            "the existing value on disk'."
        ),
    )
    secret_ref: str | None = Field(
        None, description="Override the secret filename stem."
    )


TOOL_NAME = "update_engine"
TOOL_DESCRIPTION = (
    "Create or update an engine JSON config. Secrets round-trip via "
    "the sentinel '***' so masked reads survive edits."
)
ARGS_MODEL = UpdateEngineArgs


async def run(client: WebApiClient, args: UpdateEngineArgs) -> Any:
    """Call ``PUT /api/v1/engines/{name}``."""
    body: dict[str, Any] = {
        "type": args.type,
        "args": args.args,
        "secrets": args.secrets,
    }
    if args.secret_ref:
        body["secret_ref"] = args.secret_ref
    return await client.put(f"/api/v1/engines/{args.name}", json_body=body)
