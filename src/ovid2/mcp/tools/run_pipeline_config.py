"""``run_pipeline_config`` — submit a pipeline_config as an ETL job right now."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class RunPipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the pipeline_config to run.")


TOOL_NAME = "run_pipeline_config"
TOOL_DESCRIPTION = (
    "Convert the named pipeline_config into a JobSubmitRequest and enqueue it. "
    "Bypasses the trigger cadence — the pipeline_config runs immediately."
)
ARGS_MODEL = RunPipelineConfigArgs


async def run(client: WebApiClient, args: RunPipelineConfigArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{name}/run``."""
    return await client.post(f"/api/v1/pipelines/{args.name}/run", json_body={})
