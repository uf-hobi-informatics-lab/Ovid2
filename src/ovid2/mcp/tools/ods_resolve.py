"""``ods_resolve`` — map a Data Space path to its storage account + exact location."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsResolveArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str = Field(
        ...,
        description="Data Space path (e.g. '/inbox/landing/2026_03' or '/sf_prod/CHAR_DB/SITE_X').",
    )


TOOL_NAME = "ods_resolve"
TOOL_DESCRIPTION = (
    "Resolve an Ovid Data Space path to its storage account, the exact location "
    "under it, the batch id a run reading it gets, and the saved address that "
    "points exactly there (if any). No I/O is performed. Response shape: "
    "OdsResolveResult { path, connection, type, location, batch_id, "
    "address_name, candidates }."
)
ARGS_MODEL = OdsResolveArgs


async def run(client: WebApiClient, args: OdsResolveArgs) -> Any:
    """Call ``GET /api/v1/dataspace/resolve``."""
    return await client.get("/api/v1/dataspace/resolve", path=args.path)
