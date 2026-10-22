"""``rerun_job`` — byte-identical retry of a finished job."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RerunJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Finished job id to rerun.")


TOOL_NAME = "rerun_job"
TOOL_DESCRIPTION = (
    "Enqueue a byte-identical retry of a finished job. The tracker "
    "row for the batch is reset to NEW; a new job_id is returned with "
    "parent_job_id set to the original."
)
ARGS_MODEL = RerunJobArgs


async def run(client: WebApiClient, args: RerunJobArgs) -> Any:
    """Call ``POST /api/v1/jobs/{job_id}/rerun``."""
    return await client.post(f"/api/v1/jobs/{args.job_id}/rerun")
