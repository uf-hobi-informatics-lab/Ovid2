"""``list_transforms`` — the public transformations library catalogue."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ListTransformsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    category: str | None = Field(
        default=None,
        description="Filter by category: formatting / correction / reshape / sampling.",
    )


TOOL_NAME = "list_transforms"
TOOL_DESCRIPTION = (
    "List the public, CDM-agnostic transformations library — the menu of "
    "general-purpose lazy DataFrame operations (formatting, correction, "
    "reshape, sampling) that can be composed into reusable transform "
    "chains. Returns each transform's name, summary, category, "
    "requires_family, and params_schema. Optionally filter by category."
)
ARGS_MODEL = ListTransformsArgs


async def run(client: WebApiClient, args: ListTransformsArgs) -> Any:
    """Call ``GET /api/v1/transforms``."""
    return await client.get("/api/v1/transforms", category=args.category)
