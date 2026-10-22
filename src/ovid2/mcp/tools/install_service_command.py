"""``install_service_command`` — get the sudo command for systemd install."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InstallServiceCommandArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    user: str | None = Field(None, description="Override the Unix user.")
    ovid2_bin: str | None = Field(None, description="Override the ovid2 binary path.")


TOOL_NAME = "install_service_command"
TOOL_DESCRIPTION = (
    "Return the exact `sudo ovid2 service install --user <u> --bin <b>` "
    "command the operator needs to run in a terminal. The MCP server "
    "never invokes sudo itself; agents should surface the returned "
    "command to the user."
)
ARGS_MODEL = InstallServiceCommandArgs


async def run(client: WebApiClient, args: InstallServiceCommandArgs) -> Any:
    """Call ``GET /api/v1/install/service/command``."""
    return await client.get(
        "/api/v1/install/service/command",
        user=args.user,
        ovid2_bin=args.ovid2_bin,
    )
