"""``storage_browse`` — list a storage account's folder tree."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class StorageBrowseArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Storage-account (connection) name.")
    prefix: str | None = Field(
        None,
        description=(
            "Sub-path / prefix to descend into. Omit or null to list "
            "the storage account root. Pass a `dir` entry's `path` "
            "from a previous call to navigate deeper."
        ),
    )


TOOL_NAME = "storage_browse"
TOOL_DESCRIPTION = (
    "List one level of a storage account's contents (folders / files / "
    "tables). `dir` entries are navigable — pass their `path` back as "
    "`prefix`. Returns supported=False when the storage type cannot be "
    "browsed. Powers the graphical pipeline/folder picker on the "
    "pipeline submit page."
)
ARGS_MODEL = StorageBrowseArgs


async def run(client: WebApiClient, args: StorageBrowseArgs) -> Any:
    """Call ``GET /api/v1/storage/{name}/browse``."""
    return await client.get(
        f"/api/v1/storage/{args.name}/browse", prefix=args.prefix
    )
