"""``validate_mappers`` — JSON-schema check every mapper YAML for one pipeline."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ValidateMappersArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")


TOOL_NAME = "validate_mappers"
TOOL_DESCRIPTION = (
    "Run the packaged JSON-schema validator against every mapper YAML "
    "in a pipeline package."
)
ARGS_MODEL = ValidateMappersArgs


async def run(client: WebApiClient, args: ValidateMappersArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{pipeline}/mappers:validate``."""
    return await client.post(
        f"/api/v1/pipelines/{args.pipeline}/mappers:validate"
    )
