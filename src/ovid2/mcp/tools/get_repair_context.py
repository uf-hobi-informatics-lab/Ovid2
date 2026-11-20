"""``get_repair_context`` — failed health checks + remediation hints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class GetRepairContextArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    instance_id: str = Field(..., description="Instance id of the health run (from a run record).")


TOOL_NAME = "get_repair_context"
TOOL_DESCRIPTION = (
    "Return the repair context for a health run: every failed check "
    "ordered by severity (blocker / warning / info), its aggregate "
    "exception metrics, and the domain pack's remediation hint plus the "
    "fixers / transforms that typically repair it. Aggregate only — no "
    "row values. Use it to *propose* a fix (transform chain, fixer "
    "config, re-run); a human applies it."
)
ARGS_MODEL = GetRepairContextArgs


async def run(client: WebApiClient, args: GetRepairContextArgs) -> Any:
    """Call ``GET /api/v1/runs/{instance_id}/repair-context``."""
    return await client.get(f"/api/v1/runs/{args.instance_id}/repair-context")
