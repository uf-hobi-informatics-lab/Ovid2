"""``ods_describe_table`` — metadata of one Ovid Data Space table."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsDescribeTableArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str = Field(
        ...,
        description=(
            "Data Space path of a table / data file as listed by ods_list, e.g. "
            "'/inbox/landing/2026_03/DEMOGRAPHIC.csv' or '/sf_prod/CDM_DB/SITE_X/DEMOGRAPHIC'."
        ),
    )
    refresh: bool = Field(False, description="Bypass the 60 s server-side detail cache.")


TOOL_NAME = "ods_describe_table"
TOOL_DESCRIPTION = (
    "Describe one Ovid Data Space table / data file — metadata only, never row "
    "values: fields and types, row count (exact or a statistics estimate), "
    "size, last modified, created, part files, delimiter, and for databases "
    "the table definition (DDL). Response shape: OdsTableDetail { path, "
    "connection, type, location, name, kind, format, columns: [{name, type, "
    "nullable}], row_count, row_count_exact, size_bytes, modified_at, "
    "created_at, ddl, supported, previewable, message }."
)
ARGS_MODEL = OdsDescribeTableArgs


async def run(client: WebApiClient, args: OdsDescribeTableArgs) -> Any:
    """Call ``GET /api/v1/dataspace/table?path=…``."""
    params: dict[str, Any] = {"path": args.path}
    if args.refresh:
        params["refresh"] = "true"
    return await client.get("/api/v1/dataspace/table", **params)
