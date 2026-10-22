"""``fs_roots`` — curated starting points for the filesystem picker."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "fs_roots"
TOOL_DESCRIPTION = (
    "Return the curated starting points offered by the filesystem "
    "picker: the operator's $HOME, every allowlist root from "
    "``settings.fs_picker.roots``, every existing disc-connection "
    "host_path, and ``/`` as a last-resort. Read-only. When the "
    "picker is disabled the response has ``enabled=False`` and "
    "``roots=[]``."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/fs/roots``."""
    return await client.get("/api/v1/fs/roots")
