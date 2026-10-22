"""``run_timeline`` — aggregate stage/table/fix timeline for one run."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RunTimelineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    instance_id: str = Field(..., description="Run instance id.")


TOOL_NAME = "run_timeline"
TOOL_DESCRIPTION = (
    "Return an aggregated view of the full event log — stage counters, "
    "per-table status, and applied/skipped/failed fixes."
)
ARGS_MODEL = RunTimelineArgs


async def run(client: WebApiClient, args: RunTimelineArgs) -> Any:
    """Call ``GET /api/v1/runs/{instance_id}/timeline``."""
    return await client.get(f"/api/v1/runs/{args.instance_id}/timeline")
