"""``get_mapper`` — raw + parsed contents of one mapper spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetMapperArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (mapper spec stem).")


TOOL_NAME = "get_mapper"
TOOL_DESCRIPTION = (
    "Return raw text plus parsed YAML/dict contents for one mapper "
    "spec (yaml or python source-kind)."
)
ARGS_MODEL = GetMapperArgs


async def run(client: WebApiClient, args: GetMapperArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}/mappers/{table}``."""
    return await client.get(
        f"/api/v1/pipelines/{args.pipeline}/mappers/{args.table}"
    )
