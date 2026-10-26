"""``install_image_build_status`` — inspect one image-build row."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class InstallImageBuildStatusArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    build_id: str = Field(..., description="Build id returned by install_image_build.")
    include_log: bool = Field(
        False,
        description="When true, also fetch the captured docker log as a plain string.",
    )


TOOL_NAME = "install_image_build_status"
TOOL_DESCRIPTION = (
    "Return the ImageBuildDetail row for one docker build. Set "
    "include_log=true to also fetch the captured stdout/stderr (in a "
    "'log' field of the response)."
)
ARGS_MODEL = InstallImageBuildStatusArgs


async def run(client: WebApiClient, args: InstallImageBuildStatusArgs) -> Any:
    """Fetch the row and, optionally, the log."""
    detail = await client.get(f"/api/v1/install/image/builds/{args.build_id}")
    if args.include_log:
        log_text = await client.get_text(f"/api/v1/install/image/builds/{args.build_id}/log")
        # Merge into a single dict for the client's convenience.
        if isinstance(detail, dict):
            detail = dict(detail)
            detail["log"] = log_text
    return detail
