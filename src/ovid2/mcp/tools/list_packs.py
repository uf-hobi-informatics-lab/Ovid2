"""``list_packs`` — the domain-pack catalogue."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ovid2.mcp.client import WebApiClient


class ListPacksArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME` (none)."""


TOOL_NAME = "list_packs"
TOOL_DESCRIPTION = (
    "List the domain packs (built-in, installed and folder packs): each "
    "pack's id, version, domain, origin, whether it is enabled / active, "
    "and what it provides (CDMs, health checks, report-cycle packages, "
    "vocabularies, report templates). Read-only."
)
ARGS_MODEL = ListPacksArgs


async def run(client: WebApiClient, args: ListPacksArgs) -> Any:
    """Call ``GET /api/v1/packs``."""
    del args
    return await client.get("/api/v1/packs")
