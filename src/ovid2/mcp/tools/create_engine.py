"""``create_engine`` — create a new engine JSON config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CreateEngineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem for the new engine.")
    type: str = Field(
        ...,
        description="Engine type — one of pyspark, databricks_spark, snowpark.",
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Non-secret compute knobs (spark.executor.memory, workspace_url, "
            "warehouse, ...). See list_engine_types for the per-type schema."
        ),
    )
    secrets: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Credential material (Databricks token, Snowflake password / "
            "private_key). Written to secrets.d/*.json with mode 0o600; "
            "the engine JSON only holds a secret_ref."
        ),
    )
    secret_ref: str | None = Field(
        None,
        description=("Override the auto-generated secret file stem (default: engine_<name>)."),
    )


TOOL_NAME = "create_engine"
TOOL_DESCRIPTION = (
    "Create a new engine JSON config. Fails with HTTP 400 when an "
    "engine of the same name already exists. Use update_engine for "
    "idempotent create-or-update."
)
ARGS_MODEL = CreateEngineArgs


async def run(client: WebApiClient, args: CreateEngineArgs) -> Any:
    """Call ``POST /api/v1/engines/{name}``."""
    body: dict[str, Any] = {
        "type": args.type,
        "args": args.args,
        "secrets": args.secrets,
    }
    if args.secret_ref:
        body["secret_ref"] = args.secret_ref
    return await client.post(f"/api/v1/engines/{args.name}", json_body=body)
