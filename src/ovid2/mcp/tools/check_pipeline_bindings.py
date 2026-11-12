"""``check_pipeline_bindings`` — cross-configuration name checks for a package."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CheckPipelineBindingsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package name.")
    engine: str | None = Field(None, description="Engine to check family compatibility against.")


TOOL_NAME = "check_pipeline_bindings"
TOOL_DESCRIPTION = (
    "Check every name a pipeline package depends on against the rest of Ovid2 — "
    "registered CDM, identity constants, engine family, secrets.d salt keys, "
    "dictionaries.py names, fixer names / reference tables / list files, formatter "
    "class paths and vocabularies, transform assignments. Returns "
    "[{id, category, label, value, must_match, status, detail, where}]. Read-only."
)
ARGS_MODEL = CheckPipelineBindingsArgs


async def run(client: WebApiClient, args: CheckPipelineBindingsArgs) -> Any:
    """Call ``GET /api/v1/pipeline-packages/{pipeline}/bindings``."""
    return await client.get(
        f"/api/v1/pipeline-packages/{args.pipeline}/bindings", engine=args.engine
    )
