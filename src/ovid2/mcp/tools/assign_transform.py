"""``assign_transform`` — run a library chain in a pipeline's transform stage."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class AssignTransformArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Library chain name.")
    pipeline: str = Field(..., description="Pipeline package to assign it to.")
    tables: list[str] = Field(..., min_length=1, description="CDM tables, or ['*'] for all.")


TOOL_NAME = "assign_transform"
TOOL_DESCRIPTION = (
    "Assign a transform chain to tables of a pipeline: appends {spec: name} to "
    "<pipeline>/transforms/assignments.yaml so the pipeline's `transform` stage "
    "(between deduplicate and map) runs it. Idempotent. Requires manage_config."
)
ARGS_MODEL = AssignTransformArgs


async def run(client: WebApiClient, args: AssignTransformArgs) -> Any:
    """Call ``POST /api/v1/transform-library/{name}/assign``."""
    return await client.post(
        f"/api/v1/transform-library/{args.name}/assign",
        json_body={"pipeline": args.pipeline, "tables": args.tables},
    )
