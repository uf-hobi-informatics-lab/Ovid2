"""``scaffold_pipeline`` — create a pipeline package from a template."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ScaffoldPipelineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Name for the new pipeline package.")
    template: str | None = Field(
        None,
        description=(
            "Pipeline template id (from list_pipeline_templates). "
            "Supply this OR both source_pipeline and cdm."
        ),
    )
    source_pipeline: str | None = Field(
        None,
        validation_alias=AliasChoices("source_pipeline", "source_pipeline"),
        description="Explicit source pipeline to clone (used with cdm). "
        "'source_pipeline' is a deprecated alias.",
    )
    cdm: str | None = Field(
        None,
        description="Target CDM name (required with source_pipeline).",
    )
    identity: dict[str, Any] | None = Field(
        None,
        description=(
            "Identity constants for the clone (NETWORKID, DATAMARTID, DATAVANT_KEY, "
            "MERGE_DATABASE_NAME, …). Unset keys get name-derived defaults — call "
            "inspect_pipeline_template first to see which keys the template declares."
        ),
    )


TOOL_NAME = "scaffold_pipeline"
TOOL_DESCRIPTION = (
    "Create a new pipeline package by cloning a pipeline template (or an "
    "explicit source pipeline) under a new name. The clone inherits the "
    "template's formatters, dictionaries, and fixer chains; its "
    "pipeline_settings identity fields are reset to name-derived "
    "placeholders (or the supplied identity values), every reference to "
    "the template package is rewritten to the clone, and the target CDM "
    "binding is preserved. Fails with "
    "HTTP 400 when the name already exists or the template is unknown."
)
ARGS_MODEL = ScaffoldPipelineArgs


async def run(client: WebApiClient, args: ScaffoldPipelineArgs) -> Any:
    """Call ``POST /api/v1/pipeline-packages/scaffold``.

    The server-side scaffold payload still uses the ``source_pipeline``
    key, so map the (canonical) ``source_pipeline`` field back onto it.
    """
    payload = args.model_dump(exclude_none=True)
    if "source_pipeline" in payload:
        payload["source_pipeline"] = payload.pop("source_pipeline")
    return await client.post("/api/v1/pipeline-packages/scaffold", json_body=payload)
