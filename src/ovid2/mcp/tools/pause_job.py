"""``pause_job`` — pause a job at its table boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class PauseJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Job id to pause.")


TOOL_NAME = "pause_job"
TOOL_DESCRIPTION = (
    "Pause a queued or running job at its next table boundary: the table "
    "that is running finishes, then the job holds (clocks frozen) until "
    "resume_job or cancel_job. One-shot kinds (export, workflow storage / "
    "probe) cannot pause."
)
ARGS_MODEL = PauseJobArgs


async def run(client: WebApiClient, args: PauseJobArgs) -> Any:
    """Call ``POST /api/v1/jobs/{job_id}/pause``."""
    return await client.post(f"/api/v1/jobs/{args.job_id}/pause")
