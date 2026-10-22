"""``install_bootstrap`` — create the XDG tree and drop sample configs."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "install_bootstrap"
TOOL_DESCRIPTION = (
    "Run the XDG bootstrap step of the install wizard (idempotent). "
    "Creates XDG_CONFIG_HOME/ovid2/, secrets.d/ (0o700), sample disc "
    "configs, and a snowflake.json template (0o600). Returns the "
    "ordered log lines returned by the bootstrap helper."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``POST /api/v1/install/bootstrap``."""
    return await client.post("/api/v1/install/bootstrap", json_body={})
