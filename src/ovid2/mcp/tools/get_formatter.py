"""``get_formatter`` — raw + parsed contents of one formatter spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetFormatterArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (formatter spec stem).")


TOOL_NAME = "get_formatter"
TOOL_DESCRIPTION = (
    "Return raw text plus parsed YAML/dict contents for one formatter "
    "spec (yaml or python source-kind)."
)
ARGS_MODEL = GetFormatterArgs


async def run(client: WebApiClient, args: GetFormatterArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}/formatters/{table}``."""
    return await client.get(f"/api/v1/pipelines/{args.pipeline}/formatters/{args.table}")
