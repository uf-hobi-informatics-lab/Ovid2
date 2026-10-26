"""``ods_list`` — list one level of the Ovid Data Space."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsListArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str = Field(
        "/",
        description=(
            "Data Space path. '/' lists storage accounts; '/<account>/<folder…>' "
            "lists a folder (disc / s3 / azure_blob / gcs) or DATABASE/SCHEMA "
            "(warehouses), e.g. '/inbox/landing' or '/sf_prod/CHAR_DB'."
        ),
    )


TOOL_NAME = "ods_list"
TOOL_DESCRIPTION = (
    "List the children of an Ovid Data Space path. Every storage account is a "
    "tree of plain directories; the complete path of a folder / schema is the "
    "batch id of a run that reads it, and an address pointing at it is the "
    "exact location its data is read from / written to. Response shape: "
    "OdsListing { path, connection, type, location, batch_id, address_name, "
    "supported, message, entries: [{name, kind, path, type, size_bytes}] }."
)
ARGS_MODEL = OdsListArgs


async def run(client: WebApiClient, args: OdsListArgs) -> Any:
    """Call ``GET /api/v1/dataspace?path=…``."""
    return await client.get("/api/v1/dataspace", path=args.path or "/")
