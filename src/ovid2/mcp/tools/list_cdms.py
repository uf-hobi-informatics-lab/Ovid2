"""``list_cdms`` — enumerate registered CDMs (C1)."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_cdms"
TOOL_DESCRIPTION = (
    "Enumerate every CDM registered in ovid2.cdm.registry. Returns a "
    "list of {name, version, table_count, has_yaml} objects. Consumed "
    "by the pipelines editor to populate the CDM selector and by clients "
    "that need to check which CDMs Ovid2 knows about before creating a "
    "pipeline with kind='cdm'."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/cdms``."""
    return await client.get("/api/v1/cdms")
