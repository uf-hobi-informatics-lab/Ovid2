"""``inspect_pipeline_template`` — what a template provides + names to match."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InspectPipelineTemplateArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    template: str = Field(..., description="Template id (from list_pipeline_templates).")
    name: str | None = Field(None, description="Planned clone name (drives identity defaults).")
    engine: str | None = Field(None, description="Engine to check family compatibility against.")


TOOL_NAME = "inspect_pipeline_template"
TOOL_DESCRIPTION = (
    "Inspect a pipeline template before scaffolding: every CDM table with its "
    "formatter / source-file patterns / mapper / fix chain / assigned transforms, "
    "the identity constants (current value + the clone's default), the required "
    "engine family, and every name that must match other configuration "
    "(bindings with ok/warn/error status). Read-only."
)
ARGS_MODEL = InspectPipelineTemplateArgs


async def run(client: WebApiClient, args: InspectPipelineTemplateArgs) -> Any:
    """Call ``GET /api/v1/pipeline-templates/{template}``."""
    return await client.get(
        f"/api/v1/pipeline-templates/{args.template}", name=args.name, engine=args.engine
    )
