"""``list_health_checks`` — the data-health / EDC check registry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ovid2.mcp.client import WebApiClient


class ListHealthChecksArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME` (none)."""


TOOL_NAME = "list_health_checks"
TOOL_DESCRIPTION = (
    "List the registered data-health checks (PCORnet EDC / Data "
    "Curation Query ports plus Ovid2-native panels). Returns each "
    "check's id, name, part, operator, and description."
)
ARGS_MODEL = ListHealthChecksArgs


async def run(client: WebApiClient, args: ListHealthChecksArgs) -> Any:
    """Call ``GET /api/v1/health/checks``."""
    del args
    return await client.get("/api/v1/health/checks")
