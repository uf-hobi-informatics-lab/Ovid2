"""``get_health_report`` — a run's data-health report artifact."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetHealthReportArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    instance_id: str = Field(..., description="Instance id of the health run (from a run record).")


TOOL_NAME = "get_health_report"
TOOL_DESCRIPTION = (
    "Return the data-health report artifact for a run — the per-check "
    "summary + metric rows produced by an ``ovid2 health`` run. 404 "
    "when the run was not a health run."
)
ARGS_MODEL = GetHealthReportArgs


async def run(client: WebApiClient, args: GetHealthReportArgs) -> Any:
    """Call ``GET /api/v1/runs/{instance_id}/report``."""
    return await client.get(f"/api/v1/runs/{args.instance_id}/report")
