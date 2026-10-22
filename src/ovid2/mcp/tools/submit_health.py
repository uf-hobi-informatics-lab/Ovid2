"""``submit_health`` — enqueue an ``ovid2 health`` data-health run.

Dedicated MCP tool for the data-health / PCORnet EDC checks. Under the
hood this is a ``submit_run`` with ``kind='health'`` — clients that
prefer the generic surface can call ``submit_run`` directly; this tool
exists so LLM callers can discover the data-health capability by name.

The route through the web server is ``POST /api/v1/runs`` with
``kind='health'``. The run reads the CDM tables at ``input_address`` as
they are (no formatter/mapper/fixer chain), writes a durable
``dcq_results`` metric-row table to ``output_address``, and drops a JSON
report artifact the viewer renders at ``/runs/{instance_id}/report``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SubmitHealthArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`.

    ``kind`` is not exposed — it is hardcoded to ``health`` in
    :func:`run` — so a caller cannot accidentally submit a different
    job kind through this tool.
    """

    pipeline: str = Field(
        "pcornet_edc",
        description=(
            "Pipeline label. Defaults to the generic 'pcornet_edc' EDC label "
            "(CDM pcornet_7_0) so one reusable health run can target any "
            "PCORnet database — the database/schema come from the selected "
            "storage account, not a per-partner package. Pass a partner name "
            "only if you need its CDM/default-database behaviour."
        ),
    )
    batch_id: str = Field(..., description="Batch / refresh identifier (e.g. 20260101).")
    input_address: str = Field(
        "",
        description=(
            "Saved source address to run the checks against (CDM tables). "
            "Leave blank and use input_connection + input_location to select "
            "the database/schema at run time."
        ),
    )
    output_address: str = Field(
        "",
        description=(
            "Saved address for the durable dcq_results table. Leave blank and "
            "use output_connection + output_location to select at run time."
        ),
    )
    input_connection: str = Field(
        "",
        description=(
            "Storage account (connection) name for the input, used with "
            "input_location instead of a saved input_address. Discover "
            "accounts via list_connections and enumerate their databases/"
            "schemas via browse_storage."
        ),
    )
    input_location: str = Field(
        "",
        description=(
            "Location under input_connection, e.g. 'DATABASE/SCHEMA' for "
            "Snowflake or a folder path for object stores."
        ),
    )
    output_connection: str = Field(
        "",
        description="Storage account (connection) name for the results output.",
    )
    output_location: str = Field(
        "",
        description="Location under output_connection, e.g. 'DATABASE/SCHEMA'.",
    )
    parts: list[str] = Field(
        default_factory=lambda: ["all"],
        description=(
            "Check parts to run (part1..part4, mapping, freshness, "
            "integrity, completeness, semantic) or ['all']."
        ),
    )
    packages: list[str] = Field(
        default_factory=list,
        description=(
            "EDC/DC report-cycle packages to run (e.g. ['20r1']); expands "
            "to the union of the packages' member checks. Overrides parts; "
            "itself overridden by checks. Discover ids via "
            "list_health_packages."
        ),
    )
    checks: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit check ids/names to run (e.g. ['1.01', 'DC_3_09']); "
            "overrides packages and parts when non-empty."
        ),
    )
    prev_input_address: str | None = Field(
        None,
        description="Prior-refresh input address for the freshness / volume-trend panel.",
    )
    cdm: str | None = Field(
        None, description="CDM name (defaults to the pipeline's CDM when omitted)."
    )
    engine: str = Field("pyspark", description="Compute engine (default: pyspark).")


TOOL_NAME = "submit_health"
TOOL_DESCRIPTION = (
    "Enqueue an ``ovid2 health`` data-health run — the PCORnet EDC / "
    "Data Curation Query checks plus Ovid2-native health panels. Reads "
    "the CDM tables at input_address, writes a durable dcq_results table "
    "to output_address, and produces a navigable report at "
    "/runs/{instance_id}/report. Returns the job_id and instance_id."
)
ARGS_MODEL = SubmitHealthArgs


async def run(client: WebApiClient, args: SubmitHealthArgs) -> Any:
    """Call ``POST /api/v1/runs`` with ``kind='health'``."""
    payload = args.model_dump()
    payload["kind"] = "health"
    return await client.post("/api/v1/runs", json_body=payload)
