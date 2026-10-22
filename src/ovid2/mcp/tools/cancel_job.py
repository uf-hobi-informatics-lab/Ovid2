"""``cancel_job`` — request cancellation of a queued or running job."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CancelJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Job id to cancel.")


TOOL_NAME = "cancel_job"
TOOL_DESCRIPTION = (
    "Request cooperative cancellation of a queued or running job. "
    "Queued jobs are cancelled immediately; running jobs are sent "
    "SIGTERM and cleaned up by the worker on the next poll."
)
ARGS_MODEL = CancelJobArgs


async def run(client: WebApiClient, args: CancelJobArgs) -> Any:
    """Call ``POST /api/v1/jobs/{job_id}/cancel``."""
    return await client.post(f"/api/v1/jobs/{args.job_id}/cancel")
