"""``save_transform`` — create/update a user transform-library chain."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SaveTransformArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Chain name (lower-case, digits, _).")
    raw_text: str = Field(
        ...,
        description=(
            "Full YAML: {v: 1, description: ..., steps: [{op: <name>, ...params}]}. "
            "derive_column / filter_expr take a Spark-SQL row expression "
            "(allow-listed functions only)."
        ),
    )
    pipeline: str | None = Field(
        None, description="When set, save as a private chain of this pipeline package instead."
    )


TOOL_NAME = "save_transform"
TOOL_DESCRIPTION = (
    "Create or update a reusable transform chain (a user-defined function built "
    "from library ops). Validated server-side: schema, each op's params_schema, "
    "and the SQL expression allow-list (no sub-queries / aggregates / reflect / "
    "UDFs). Saves to the user library, or as a pipeline-private chain when "
    "`pipeline` is given. Requires manage_config."
)
ARGS_MODEL = SaveTransformArgs


async def run(client: WebApiClient, args: SaveTransformArgs) -> Any:
    """Call ``PUT /api/v1/transform-library/{name}`` (or the pipeline-private path)."""
    body = {"raw_text": args.raw_text}
    if args.pipeline:
        return await client.put(
            f"/api/v1/pipeline-packages/{args.pipeline}/transforms/{args.name}", json_body=body
        )
    return await client.put(f"/api/v1/transform-library/{args.name}", json_body=body)
