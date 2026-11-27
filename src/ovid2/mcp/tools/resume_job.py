"""``resume_job`` — resume a job at its table boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ResumeJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Job id to resume.")


TOOL_NAME = "resume_job"
TOOL_DESCRIPTION = "Resume a paused job; it continues with its next table."
ARGS_MODEL = ResumeJobArgs


async def run(client: WebApiClient, args: ResumeJobArgs) -> Any:
    """Call ``POST /api/v1/jobs/{job_id}/resume``."""
    return await client.post(f"/api/v1/jobs/{args.job_id}/resume")
