"""``save_mapper`` — write/validate a mapper YAML spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SaveMapperArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (mapper spec stem).")
    raw_text: str = Field(..., description="Full YAML source (UTF-8).")


TOOL_NAME = "save_mapper"
TOOL_DESCRIPTION = (
    "Create or update a mapper YAML spec. The service parses the YAML "
    "and validates against the packaged JSON schema before touching "
    "disk; invalid payloads are refused with HTTP 400."
)
ARGS_MODEL = SaveMapperArgs


async def run(client: WebApiClient, args: SaveMapperArgs) -> Any:
    """Call ``PUT /api/v1/pipelines/{pipeline}/mappers/{table}``."""
    return await client.put(
        f"/api/v1/pipelines/{args.pipeline}/mappers/{args.table}",
        json_body={"raw_text": args.raw_text},
    )
