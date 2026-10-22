"""``enable_pipeline_config`` — mark a pipeline_config as enabled."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class EnablePipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the pipeline_config.")


TOOL_NAME = "enable_pipeline_config"
TOOL_DESCRIPTION = "Set enabled=true on a pipeline_config so its trigger fires."
ARGS_MODEL = EnablePipelineConfigArgs


async def run(client: WebApiClient, args: EnablePipelineConfigArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{name}/enable``."""
    return await client.post(f"/api/v1/pipelines/{args.name}/enable", json_body={})
