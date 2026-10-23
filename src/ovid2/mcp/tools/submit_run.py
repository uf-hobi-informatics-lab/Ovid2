"""``submit_run`` — enqueue a new job (any kind)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SubmitRunArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    kind: str = Field(
        "etl",
        description=(
            "Job kind. One of `etl`, `analysis`, `dq_checks`, `copy`, "
            "`move`. `etl` (default) runs `ovid2 etl`. `copy` runs "
            "`ovid2 tools copy` — bypasses the formatter, mapper, and "
            "fixer stages, takes an explicit `tables` list or `all` "
            "(expanded server-side for listable sources), and ignores `transformations` / `workers` "
            "/ `sf_*` / `cdm` / `stderr_log`. `move` runs "
            "`ovid2 tools move` — the driver-native successor to "
            "`copy` with the same shape rules plus `strict_types` "
            "support (D6.0 advisory CDM type check). The deprecated "
            "alias `cdm` is accepted and normalised to `etl`."
        ),
    )
    cdm: str | None = Field(
        None,
        description="CDM name (only honoured when kind=etl).",
    )
    pipeline: str = Field(..., description="Pipeline name (must exist under ovid2_pipelines).")
    batch_id: str = Field(..., description="Batch identifier (e.g. 20260101).")
    input_address: str = Field(..., description="Filename stem of the input address config.")
    output_address: str = Field(..., description="Filename stem of the output address config.")
    tables: list[str] = Field(
        default_factory=lambda: ["all"],
        description=(
            "Tables to process, or ['all']. Copy and move jobs require "
            "an explicit list — ['all'] is rejected server-side. For "
            "kind=copy and kind=move each entry is either a bare "
            "logical name ('HASH_TOKEN') or a 'LOGICAL=SOURCE' split "
            "('HASH_TOKEN=*datavant_person_tokens*'). LOGICAL is "
            "always used verbatim as the destination file stem — "
            "case preserved. SOURCE, when omitted, defaults to "
            "LOGICAL."
        ),
    )
    transformations: list[str] = Field(
        default_factory=lambda: ["all"],
        description="Transformations to run, or ['all']. Ignored for kind=copy or kind=move.",
    )
    engine: str = Field("pyspark", description="Compute engine (default: pyspark).")
    workers: int = Field(
        25, ge=1, le=200, description="Spark worker count. Ignored for kind=copy or kind=move."
    )
    sf_database: str | None = Field(
        None, description="Snowflake DATABASE override. Ignored for kind=copy or kind=move."
    )
    sf_account: str | None = Field(
        None, description="Snowflake ACCOUNT override. Ignored for kind=copy or kind=move."
    )
    sf_schema: str | None = Field(
        None, description="Snowflake SCHEMA override. Ignored for kind=copy or kind=move."
    )
    stderr_log: bool = Field(
        False,
        description="Mirror events to stderr as plain-text. Ignored for kind=copy or kind=move.",
    )
    extra_mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Additional bind mounts for the per-invocation container "
            "(kind=copy and kind=move only). Each entry is "
            "'HOST:CONTAINER[:MODE]' where MODE is 'rw' (default) or "
            "'ro'. Container path must not shadow the standard mount "
            "set (/app, /data, /ovid2-user-config, /ovid_credentials, "
            "/usr/include/rsa). Rejected server-side on every non-"
            "copy / non-move kind."
        ),
    )
    strict_types: bool = Field(
        False,
        description=(
            "kind=move only. Toggle the D6.0 advisory CDM type check "
            "between warn (default, False) and raise (True). Rejected "
            "server-side on every non-move kind."
        ),
    )


TOOL_NAME = "submit_run"
TOOL_DESCRIPTION = (
    "Enqueue a new job. The web worker will pick it up and spawn an "
    "isolated ovid2 subprocess whose subcommand depends on `kind` "
    "(etl→etl, analysis→analysis, dq_checks→dq_checks, copy→tools "
    "copy, move→tools move; the deprecated alias `cdm` maps to `etl`). "
    "Returns the freshly-created job_id and instance_id."
)
ARGS_MODEL = SubmitRunArgs


async def run(client: WebApiClient, args: SubmitRunArgs) -> Any:
    """Call ``POST /api/v1/runs``."""
    return await client.post("/api/v1/runs", json_body=args.model_dump())
