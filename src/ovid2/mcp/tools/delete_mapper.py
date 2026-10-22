"""``delete_mapper`` — remove a mapper YAML spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteMapperArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (mapper spec stem).")


TOOL_NAME = "delete_mapper"
TOOL_DESCRIPTION = (
    "Remove a mapper YAML spec. Python-dict SPEC modules "
    "(configuration-as-code) are never touched by this tool."
)
ARGS_MODEL = DeleteMapperArgs


async def run(client: WebApiClient, args: DeleteMapperArgs) -> Any:
    """Call ``DELETE /api/v1/pipelines/{pipeline}/mappers/{table}``."""
    return await client.delete(
        f"/api/v1/pipelines/{args.pipeline}/mappers/{args.table}"
    )
