"""``install_verify_pipeline`` — confirm a pipeline package resolves."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InstallVerifyPipelineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Pipeline package name to verify.")


TOOL_NAME = "install_verify_pipeline"
TOOL_DESCRIPTION = (
    "Confirm the requested pipeline package is importable under "
    "ovid2_pipelines.* and report its formatter/mapper counts."
)
ARGS_MODEL = InstallVerifyPipelineArgs


async def run(client: WebApiClient, args: InstallVerifyPipelineArgs) -> Any:
    """Call ``POST /api/v1/install/pipeline/verify``."""
    return await client.post("/api/v1/install/pipeline/verify", name=args.name)
