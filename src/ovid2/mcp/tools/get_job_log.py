"""``get_job_log`` — captured stderr for one job."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetJobLogArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Job id.")


TOOL_NAME = "get_job_log"
TOOL_DESCRIPTION = (
    "Return the captured subprocess stderr for one job as a plain-"
    "text string. Empty when the worker has not written any lines "
    "yet."
)
ARGS_MODEL = GetJobLogArgs


async def run(client: WebApiClient, args: GetJobLogArgs) -> str:
    """Call ``GET /api/v1/jobs/{job_id}/log``."""
    return await client.get_text(f"/api/v1/jobs/{args.job_id}/log")
