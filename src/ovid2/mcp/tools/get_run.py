"""``get_run`` — one run row by batch_id."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetRunArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    batch_id: str = Field(..., description="Batch identifier (e.g. 20260101).")


TOOL_NAME = "get_run"
TOOL_DESCRIPTION = "Return the run row for one batch_id (RunSummary shape)."
ARGS_MODEL = GetRunArgs


async def run(client: WebApiClient, args: GetRunArgs) -> Any:
    """Call ``GET /api/v1/runs/{batch_id}``."""
    return await client.get(f"/api/v1/runs/{args.batch_id}")
