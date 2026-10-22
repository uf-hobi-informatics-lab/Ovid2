"""``ods_list_tables`` — enumerate tables under a batch in ODS."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsListTablesArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline name (e.g. 'demo').")
    batch_id: str = Field(..., description="Batch identifier (e.g. '20260316').")


TOOL_NAME = "ods_list_tables"
TOOL_DESCRIPTION = (
    "List every table known under /{pipeline}/{batch_id} in the Ovid "
    "Data Space. In v1 only the disc dimension is walked. Response "
    "shape: OdsTableListing { pipeline, batch_id, tables: [{name, "
    "dimensions}] }."
)
ARGS_MODEL = OdsListTablesArgs


async def run(client: WebApiClient, args: OdsListTablesArgs) -> Any:
    """Call ``GET /api/v1/dataspace/{pipeline}/{batch_id}``."""
    return await client.get(f"/api/v1/dataspace/{args.pipeline}/{args.batch_id}")
