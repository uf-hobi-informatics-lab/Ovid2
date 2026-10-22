"""``get_pipeline_config`` — fetch one pipeline_config detail."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetPipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the pipeline_config.")


TOOL_NAME = "get_pipeline_config"
TOOL_DESCRIPTION = (
    "Return the full detail of one pipeline_config: config + runtime state "
    "(last_fired_at, next_fire_at, last_job_id)."
)
ARGS_MODEL = GetPipelineConfigArgs


async def run(client: WebApiClient, args: GetPipelineConfigArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{name}``."""
    return await client.get(f"/api/v1/pipelines/{args.name}")
