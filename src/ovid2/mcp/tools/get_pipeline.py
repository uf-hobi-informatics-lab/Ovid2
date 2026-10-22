"""``get_pipeline`` — full read-only detail for one pipeline package."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetPipelineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(
        ...,
        validation_alias=AliasChoices("pipeline", "pipeline"),
        description="Pipeline package name (e.g. 'demo'). 'pipeline' is a deprecated alias.",
    )


TOOL_NAME = "get_pipeline"
TOOL_DESCRIPTION = (
    "Return metadata plus the formatter/mapper spec roster for one "
    "pipeline package."
)
ARGS_MODEL = GetPipelineArgs


async def run(client: WebApiClient, args: GetPipelineArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}``."""
    return await client.get(f"/api/v1/pipelines/{args.pipeline}")
