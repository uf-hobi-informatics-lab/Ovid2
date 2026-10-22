"""``list_pipeline_templates`` — enumerate curated pipeline templates."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_pipeline_templates"
TOOL_DESCRIPTION = (
    "Enumerate the curated pipeline templates. Returns a list of "
    "PipelineTemplateSummary objects (id, source_pipeline, cdm, kind, "
    "label, description). Use the id with scaffold_pipeline to create a "
    "new ETL pipeline's pipeline package."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/pipeline-templates``."""
    return await client.get("/api/v1/pipeline-templates")
