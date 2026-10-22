"""``delete_pipeline_config`` — remove a pipeline_config JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class DeletePipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the pipeline_config.")


TOOL_NAME = "delete_pipeline_config"
TOOL_DESCRIPTION = "Remove a pipeline_config JSON and its runtime-state row."
ARGS_MODEL = DeletePipelineConfigArgs


async def run(client: WebApiClient, args: DeletePipelineConfigArgs) -> Any:
    """Call ``DELETE /api/v1/pipelines/{name}``."""
    return await client.delete(f"/api/v1/pipelines/{args.name}")
