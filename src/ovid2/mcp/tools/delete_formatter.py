"""``delete_formatter`` — remove a formatter YAML spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeleteFormatterArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    table: str = Field(..., description="Table name (formatter spec stem).")


TOOL_NAME = "delete_formatter"
TOOL_DESCRIPTION = (
    "Remove a formatter YAML spec. Python-dict formatter modules "
    "(configuration-as-code) are never touched by this tool."
)
ARGS_MODEL = DeleteFormatterArgs


async def run(client: WebApiClient, args: DeleteFormatterArgs) -> Any:
    """Call ``DELETE /api/v1/pipelines/{pipeline}/formatters/{table}``."""
    return await client.delete(
        f"/api/v1/pipelines/{args.pipeline}/formatters/{args.table}"
    )
