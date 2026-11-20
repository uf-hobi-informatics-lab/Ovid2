"""``get_pack`` — one domain pack and what it contributes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetPackArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pack_id: str = Field(..., description="Domain pack id, e.g. 'pcornet_7_0' (from list_packs).")


TOOL_NAME = "get_pack"
TOOL_DESCRIPTION = (
    "Return one domain pack: its manifest (version, domain, origin, "
    "requires_ovid2, provides), activation state / error, and the ids of "
    "the health checks and report-cycle packages it contributes. Use "
    "list_packs first to enumerate ids. Read-only."
)
ARGS_MODEL = GetPackArgs


async def run(client: WebApiClient, args: GetPackArgs) -> Any:
    """Call ``GET /api/v1/packs/{pack_id}``."""
    return await client.get(f"/api/v1/packs/{args.pack_id}")
