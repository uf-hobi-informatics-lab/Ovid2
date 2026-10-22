"""``preview_pipeline_file`` — read text of one pipeline file (whitelisted)."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from ovid2.mcp.client import WebApiClient


class PreviewPipelineFileArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    pipeline: str = Field(
        ...,
        validation_alias=AliasChoices("pipeline", "pipeline"),
        description="Pipeline package name. 'pipeline' is a deprecated alias.",
    )
    relative_path: str = Field(
        ...,
        description="Path relative to the pipeline package (anti-traversal enforced server-side).",
    )


TOOL_NAME = "preview_pipeline_file"
TOOL_DESCRIPTION = (
    "Return the text of one pipeline file after path-whitelisting and "
    "anti-traversal checks. Content is truncated to a bounded size; "
    "the response indicates whether truncation happened."
)
ARGS_MODEL = PreviewPipelineFileArgs


async def run(client: WebApiClient, args: PreviewPipelineFileArgs) -> Any:
    """Call ``GET /api/v1/pipelines/{pipeline}/file?relative_path=...``."""
    return await client.get(
        f"/api/v1/pipelines/{args.pipeline}/file",
        relative_path=args.relative_path,
    )
