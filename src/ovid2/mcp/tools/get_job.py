"""``get_job`` — one row of the web_jobs table."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Job id returned by submit_run.")


TOOL_NAME = "get_job"
TOOL_DESCRIPTION = "Return one job row (JobDetail shape) or 404."
ARGS_MODEL = GetJobArgs


async def run(client: WebApiClient, args: GetJobArgs) -> Any:
    """Call ``GET /api/v1/jobs/{job_id}``."""
    return await client.get(f"/api/v1/jobs/{args.job_id}")
