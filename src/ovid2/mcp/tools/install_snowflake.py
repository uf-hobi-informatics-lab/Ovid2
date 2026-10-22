"""``install_snowflake`` — persist Snowflake credentials (0o600)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InstallSnowflakeArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    values: dict[str, Any] = Field(
        ...,
        description=(
            "Snowflake secret payload. Required keys: account, user, "
            "password. Extra keys (role, warehouse, database, schema) "
            "are preserved."
        ),
    )


TOOL_NAME = "install_snowflake"
TOOL_DESCRIPTION = (
    "Write secrets.d/snowflake.json through the 0o600-enforced writer. "
    "Required keys 'account', 'user', 'password' are checked before "
    "the file is written; missing keys yield HTTP 400."
)
ARGS_MODEL = InstallSnowflakeArgs


async def run(client: WebApiClient, args: InstallSnowflakeArgs) -> Any:
    """Call ``POST /api/v1/install/snowflake``."""
    return await client.post(
        "/api/v1/install/snowflake",
        json_body={"values": args.values},
    )
