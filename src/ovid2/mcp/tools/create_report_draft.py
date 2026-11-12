"""``create_report_draft`` — create a private draft report."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CreateReportDraftArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    source_id: str = Field(..., description="Report source id.")
    template_id: str | None = Field(None, description="Template id from list_report_templates.")
    layout: dict[str, Any] | None = Field(None, description="Full report_layout.v1 document.")
    title: str | None = Field(None, max_length=160)
    folder: str = Field("", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=12)


TOOL_NAME = "create_report_draft"
TOOL_DESCRIPTION = (
    "Create a private draft report from a template or a full layout "
    "(validated against the chart library and the source). Nothing is "
    "shared or exported; returns the report and its Studio url."
)
ARGS_MODEL = CreateReportDraftArgs


async def run(client: WebApiClient, args: CreateReportDraftArgs) -> Any:
    """Call ``POST /api/v1/report-drafts``."""
    return await client.post("/api/v1/report-drafts", json_body=args.model_dump(exclude_none=True))
