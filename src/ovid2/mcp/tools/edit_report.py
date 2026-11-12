"""``edit_report`` — apply Report Studio layout edit ops."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class EditReportArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    report_id: str = Field(..., description="Report id.")
    ops: list[dict[str, Any]] = Field(
        ..., min_length=1, max_length=40, description="Layout edit ops."
    )
    expected_version: int | None = Field(None, description="Reject (409) if the report changed.")
    save: bool = Field(
        False, description="False (default) = validate and return the new layout only."
    )


TOOL_NAME = "edit_report"
TOOL_DESCRIPTION = (
    "Apply Report Studio layout edit ops (set_report, "
    "add/update/move/remove section or block, set_visibility). Defaults "
    "to a dry run that validates and returns the new layout; save=true "
    "stores a new version."
)
ARGS_MODEL = EditReportArgs


async def run(client: WebApiClient, args: EditReportArgs) -> Any:
    """Call ``POST /api/v1/reports/{report_id}/ops``."""
    return await client.post(
        f"/api/v1/reports/{quote(args.report_id, safe='')}/ops",
        json_body={"ops": args.ops, "expected_version": args.expected_version, "save": args.save},
    )
