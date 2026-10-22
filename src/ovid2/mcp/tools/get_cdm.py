"""``get_cdm`` — fetch full detail for one registered CDM (C1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetCdmArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="CDM identifier, e.g. 'pcornet_7_0'.")


TOOL_NAME = "get_cdm"
TOOL_DESCRIPTION = (
    "Return full metadata for one CDM registered in ovid2.cdm.registry. "
    "Includes the ordered table list with composite dedup keys, id "
    "columns (patient/encounter/provider roles), reference tables, "
    "flavour-of-null sentinels, shared views, and registered "
    "valuesets. Use list_cdms first to enumerate known names."
)
ARGS_MODEL = GetCdmArgs


async def run(client: WebApiClient, args: GetCdmArgs) -> Any:
    """Call ``GET /api/v1/cdms/{name}``."""
    return await client.get(f"/api/v1/cdms/{args.name}")
