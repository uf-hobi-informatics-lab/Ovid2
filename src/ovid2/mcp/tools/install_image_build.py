"""``install_image_build`` — start a background docker build."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InstallImageBuildArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    tag: str | None = Field(
        None,
        description="Image tag (defaults to ovid2.config.settings.DOCKER_IMAGE).",
    )
    context: str | None = Field(
        None, description="Build-context directory (defaults to the repo-local checkout)."
    )
    dockerfile: str | None = Field(
        None, description="Path to a Dockerfile (defaults to <context>/docker/Dockerfile)."
    )


TOOL_NAME = "install_image_build"
TOOL_DESCRIPTION = (
    "Kick off a docker build in the background. Returns the "
    "ImageBuildDetail row for the new build (build_id, status, etc.); "
    "poll install_image_build_status for progress."
)
ARGS_MODEL = InstallImageBuildArgs


async def run(client: WebApiClient, args: InstallImageBuildArgs) -> Any:
    """Call ``POST /api/v1/install/image/build``."""
    return await client.post(
        "/api/v1/install/image/build",
        json_body=args.model_dump(),
    )
