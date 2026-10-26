"""``create_pipeline_config`` — create a new pipeline_config JSON."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class PipelineConfigTriggerArgs(BaseModel):
    """Trigger cadence for a pipeline_config."""

    type: str = Field(..., description="'manual', 'cron', or 'interval'.")
    cron: str | None = Field(None, description="Crontab expression when type='cron'.")
    interval_minutes: int | None = Field(
        None, description="Minutes between fires when type='interval'."
    )


class CreatePipelineConfigArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem for the new pipeline_config.")
    kind: str = Field(
        "etl",
        description=(
            "PipelineConfig kind. One of 'etl', 'analysis', 'dq_checks', 'copy', "
            "'move'. `copy` runs `ovid2 tools copy` (address-to-address "
            "one-shot, bypasses formatter/mapper/fixer stages); copy "
            "pipeline_configs require an explicit `tables` list ('all' is "
            "rejected) and have every pipeline-specific field scrubbed "
            "server-side (cdm, transformations, sf_*, workers). The "
            "deprecated alias 'cdm' is accepted and normalised to 'etl'."
        ),
    )
    cdm: str | None = Field(
        None,
        description=(
            "CDM name (e.g. 'pcornet_7_0'). Only meaningful when "
            "kind='etl'. Leave unset to defer resolution to the "
            "pipeline_settings.cdm fallback at pipeline start."
        ),
    )
    pipeline: str = Field(..., description="Pipeline name.")
    input_address: str = Field(
        ...,
        description=(
            "Input address stem. The address is the exact location of the data "
            "(the folder or DATABASE/SCHEMA read). For a watch trigger it is the "
            "parent folder whose new sub-folders are the batches."
        ),
    )
    output_address: str = Field(
        ..., description="Output address stem — the exact location written to."
    )
    tables: list[str] = Field(default_factory=lambda: ["all"])
    transformations: list[str] = Field(default_factory=lambda: ["all"])
    engine: str = "pyspark"
    workers: int = Field(25, ge=1, le=200)
    sf_account: str | None = None
    stderr_log: bool = False
    allow_overwrite: bool = Field(
        False,
        description=(
            "Standing permission for this config's runs to overwrite an occupied "
            "output (scheduled / watch runs reuse one output). Needs a human "
            "run.overwrite approval when set by an agent."
        ),
    )
    enabled: bool = True
    trigger: PipelineConfigTriggerArgs = Field(
        default_factory=lambda: PipelineConfigTriggerArgs(type="manual")
    )


TOOL_NAME = "create_pipeline_config"
TOOL_DESCRIPTION = (
    "Create a persisted, named ETL job template. Fails with HTTP 400 "
    "when a pipeline_config of the same name already exists."
)
ARGS_MODEL = CreatePipelineConfigArgs


async def run(client: WebApiClient, args: CreatePipelineConfigArgs) -> Any:
    """Call ``POST /api/v1/pipelines/{name}``."""
    body = args.model_dump(exclude={"name"}, exclude_none=True)
    return await client.post(f"/api/v1/pipelines/{args.name}", json_body=body)
