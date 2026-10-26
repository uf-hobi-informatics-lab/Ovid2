"""``ods_list_dimensions`` — enumerate registered ODS dimensions."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "ods_list_dimensions"
TOOL_DESCRIPTION = (
    "Return the catalogue of storage types (dimensions: disc, snowflake, "
    "s3, azure_blob, ...) and how many saved addresses use each. Response shape: "
    "OdsDimensionCatalog { default_dimension, dimensions: [{name, "
    "address_count, default_address}] }."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/dataspace/dimensions``."""
    return await client.get("/api/v1/dataspace/dimensions")
