"""``inspect_pipeline_lineage`` — the chain of functions applied to a field."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InspectPipelineLineageArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(..., description="Pipeline package (from list_pipelines).")
    table: str | None = Field(
        None, description="CDM table; omit for a per-table summary of the package."
    )


TOOL_NAME = "inspect_pipeline_lineage"
TOOL_DESCRIPTION = (
    "Field-level lineage of a pipeline package, read statically (never runs it). "
    "Without a table: each table's stages (format, deduplicate, transform, map, "
    "fix), ops and assigned chains. With a table: every CDM field and the ordered "
    "steps that touch it — op, params, columns read / written, reference tables, "
    "whether it drops rows, and the document + path that defines it. Read-only; "
    "use it to explain why a field has the value it has."
)
ARGS_MODEL = InspectPipelineLineageArgs


async def run(client: WebApiClient, args: InspectPipelineLineageArgs) -> Any:
    """Call ``GET /api/v1/pipeline-packages/{pipeline}/lineage``."""
    return await client.get(f"/api/v1/pipeline-packages/{args.pipeline}/lineage", table=args.table)
