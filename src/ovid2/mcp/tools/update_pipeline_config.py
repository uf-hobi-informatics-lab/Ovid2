"""``update_pipeline_config`` — idempotent create-or-update of a pipeline_config."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient
from ovid2.mcp.tools.create_pipeline_config import CreatePipelineConfigArgs


class UpdatePipelineConfigArgs(CreatePipelineConfigArgs):
    """Same shape as :class:`CreatePipelineConfigArgs` — separate class for MCP surface."""


TOOL_NAME = "update_pipeline_config"
TOOL_DESCRIPTION = (
    "Create or update a pipeline_config JSON. Recomputes next_fire_at from the "
    "supplied trigger; last_fired_at is preserved."
)
ARGS_MODEL = UpdatePipelineConfigArgs


async def run(client: WebApiClient, args: UpdatePipelineConfigArgs) -> Any:
    """Call ``PUT /api/v1/pipelines/{name}``."""
    body = args.model_dump(exclude={"name"}, exclude_none=True)
    return await client.put(f"/api/v1/pipelines/{args.name}", json_body=body)
