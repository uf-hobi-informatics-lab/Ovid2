"""``validate_formatters`` — JSON-schema check every formatter YAML for one pipeline."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ValidateFormattersArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")


TOOL_NAME = "validate_formatters"
TOOL_DESCRIPTION = (
    "Run the packaged JSON-schema validator against every formatter "
    "YAML in a pipeline package. Returns a FormatterValidationReport "
    "listing per-table ok/error status."
)
ARGS_MODEL = ValidateFormattersArgs


async def run(client: WebApiClient, args: ValidateFormattersArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{pipeline}/formatters:validate``."""
    return await client.post(f"/api/v1/pipelines/{args.pipeline}/formatters:validate")
