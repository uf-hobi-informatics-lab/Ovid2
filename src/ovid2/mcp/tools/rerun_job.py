"""``rerun_job`` — byte-identical retry of a finished job."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RerunJobArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    job_id: str = Field(..., description="Finished job id to rerun.")
    confirm_overwrite: bool = Field(
        False,
        description=(
            "Set True to let the run overwrite an output location that already "
            "holds tables. Without it an occupied output answers 409 "
            "overwrite_required (listing the existing tables). For an agent this "
            "becomes a run.overwrite approval request (202) a human must approve "
            "before re-submitting."
        ),
    )


TOOL_NAME = "rerun_job"
TOOL_DESCRIPTION = (
    "Enqueue a retry of a finished job (same input, output, tables). The "
    "tracker row for the batch is reset to NEW; a new job_id is returned "
    "with parent_job_id set to the original. A rerun writes to the same "
    "exact output location, so it needs confirm_overwrite when the earlier "
    "run's tables are still there."
)
ARGS_MODEL = RerunJobArgs


async def run(client: WebApiClient, args: RerunJobArgs) -> Any:
    """Call ``POST /api/v1/jobs/{job_id}/rerun``."""
    params = {"confirm_overwrite": "true"} if args.confirm_overwrite else {}
    return await client.post(f"/api/v1/jobs/{args.job_id}/rerun", **params)
