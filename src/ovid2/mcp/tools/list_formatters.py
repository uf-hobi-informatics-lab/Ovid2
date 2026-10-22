"""``list_formatters`` — list one pipeline's formatter specs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListFormattersArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")


TOOL_NAME = "list_formatters"
TOOL_DESCRIPTION = "Return the formatter file listing for one pipeline."
ARGS_MODEL = ListFormattersArgs


async def run(client: WebApiClient, args: ListFormattersArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}/formatters``."""
    return await client.get(f"/api/v1/pipelines/{args.pipeline}/formatters")
