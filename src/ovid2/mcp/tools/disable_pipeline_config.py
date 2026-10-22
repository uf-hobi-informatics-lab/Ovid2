"""``disable_pipeline_config`` — freeze a pipeline_config so its trigger stops firing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DisablePipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the pipeline_config.")


TOOL_NAME = "disable_pipeline_config"
TOOL_DESCRIPTION = "Set enabled=false on a pipeline_config; its trigger will not fire."
ARGS_MODEL = DisablePipelineConfigArgs


async def run(client: WebApiClient, args: DisablePipelineConfigArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{name}/disable``."""
    return await client.post(f"/api/v1/pipelines/{args.name}/disable", json_body={})
