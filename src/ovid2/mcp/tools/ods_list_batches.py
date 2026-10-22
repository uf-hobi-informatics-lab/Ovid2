"""``ods_list_batches`` — enumerate batches under a pipeline in ODS."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsListBatchesArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline name (e.g. 'demo').")


TOOL_NAME = "ods_list_batches"
TOOL_DESCRIPTION = (
    "List every batch known under /{pipeline} in the Ovid Data Space. "
    "In v1 only the disc dimension is walked; non-disc dimensions "
    "return empty. Response shape: OdsBatchListing { pipeline, batches: "
    "[{batch_id, dimensions}] }."
)
ARGS_MODEL = OdsListBatchesArgs


async def run(client: WebApiClient, args: OdsListBatchesArgs) -> Any:
    """Call ``GET /api/v1/dataspace/{pipeline}``."""
    return await client.get(f"/api/v1/dataspace/{args.pipeline}")
