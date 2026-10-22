"""``validate_transform`` — schema+catalogue check for a transform spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class ValidateTransformArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    spec: dict[str, Any] = Field(
        description=(
            "Candidate transform spec to validate. Shape: "
            "{'v': 1, 'steps': [{'op': <name>, ...params}]}. "
            "Use list_transforms to discover valid op names and params."
        )
    )


TOOL_NAME = "validate_transform"
TOOL_DESCRIPTION = (
    "Validate a candidate transform spec against the transform_spec JSON "
    "schema and the op catalogue before saving or running it. Returns "
    "{ok, errors}. Catches unknown op names and malformed steps early."
)
ARGS_MODEL = ValidateTransformArgs


async def run(client: WebApiClient, args: ValidateTransformArgs) -> Any:
    """Call ``POST /api/v1/transforms:validate``."""
    return await client.post("/api/v1/transforms:validate", json_body={"spec": args.spec})
