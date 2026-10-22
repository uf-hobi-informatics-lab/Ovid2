"""``list_secrets`` — enumerate secret files (metadata only)."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_secrets"
TOOL_DESCRIPTION = (
    "Enumerate every secret file under the user-scoped secrets.d/. "
    "Returns metadata (name, path, mode, size, mtime, top-level keys) "
    "but never the values themselves."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/secrets``."""
    return await client.get("/api/v1/secrets")
