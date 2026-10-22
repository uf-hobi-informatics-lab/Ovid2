"""``save_formatter`` — write/validate a formatter YAML spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SaveFormatterArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (formatter spec stem).")
    raw_text: str = Field(..., description="Full YAML source (UTF-8).")


TOOL_NAME = "save_formatter"
TOOL_DESCRIPTION = (
    "Create or update a formatter YAML spec. The service parses the "
    "YAML and validates the parsed document against the packaged JSON "
    "schema before touching disk; invalid payloads are refused with "
    "HTTP 400."
)
ARGS_MODEL = SaveFormatterArgs


async def run(client: WebApiClient, args: SaveFormatterArgs) -> Any:
    """Call ``PUT /api/v1/pipelines/{pipeline}/formatters/{table}``."""
    return await client.put(
        f"/api/v1/pipelines/{args.pipeline}/formatters/{args.table}",
        json_body={"raw_text": args.raw_text},
    )
