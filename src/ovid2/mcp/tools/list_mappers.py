"""``list_mappers`` — list one pipeline's mapper specs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListMappersArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")


TOOL_NAME = "list_mappers"
TOOL_DESCRIPTION = "Return the mapper file listing for one pipeline."
ARGS_MODEL = ListMappersArgs


async def run(client: WebApiClient, args: ListMappersArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}/mappers``."""
    return await client.get(f"/api/v1/pipelines/{args.pipeline}/mappers")
