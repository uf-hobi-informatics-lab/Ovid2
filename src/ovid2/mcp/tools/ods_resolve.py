"""``ods_resolve`` — turn an ODS path + dimension into an address name."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsResolveArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str = Field(
        ...,
        description="ODS path (e.g. '/demo/20260316/DEMOGRAPHIC').",
    )
    dim: str | None = Field(
        None,
        description=(
            "Optional dimension name (disc, snowflake, s3, azure_blob, "
            "gcs, databricks, postgres, mssql). Falls back to "
            "settings.ods.default_dimension when omitted."
        ),
    )


TOOL_NAME = "ods_resolve"
TOOL_DESCRIPTION = (
    "Resolve an Ovid Data Space path + dimension to the address "
    "config that would serve it. No I/O is performed. Response shape: "
    "OdsResolveResult { path, pipeline, batch_id, table, dimension, "
    "address_name, tier, candidates }."
)
ARGS_MODEL = OdsResolveArgs


async def run(client: WebApiClient, args: OdsResolveArgs) -> Any:
    """Call ``GET /api/v1/dataspace/resolve``."""
    kwargs: dict[str, Any] = {"path": args.path}
    if args.dim:
        kwargs["dim"] = args.dim
    return await client.get("/api/v1/dataspace/resolve", **kwargs)
