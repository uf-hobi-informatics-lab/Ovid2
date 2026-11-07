"""``ods_sample_table`` — the first rows of one Ovid Data Space table."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class OdsSampleTableArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str = Field(..., description="Data Space path of a table / data file (see ods_list).")
    limit: int = Field(25, ge=1, le=1000, description="Rows to return (1-1000).")


TOOL_NAME = "ods_sample_table"
TOOL_DESCRIPTION = (
    "Return the first rows of an Ovid Data Space table / data file (row "
    "values — may contain patient data; use only when the operator asks to "
    "see data). REQUIRES HUMAN APPROVAL: the first call returns "
    "{status: 'awaiting_approval', approval_id, action: "
    "'dataspace.agent_row_access', target}; ask the operator to approve "
    "that id in the web panel, then call again (each approval is single-"
    "use, per table). Approved response shape: OdsTableSample { path, "
    "columns: [str], rows: [[value, …]], truncated, limit, supported, message }."
)
ARGS_MODEL = OdsSampleTableArgs


async def run(client: WebApiClient, args: OdsSampleTableArgs) -> Any:
    """Call ``GET /api/v1/dataspace/table/sample?path=…&limit=…``.

    A ``202`` approval gate arrives as ``{"detail": {status:
    "awaiting_approval", …}}``; it is unwrapped so the agent sees the
    approval id at the top level.
    """
    result = await client.get("/api/v1/dataspace/table/sample", path=args.path, limit=args.limit)
    if isinstance(result, dict) and isinstance(result.get("detail"), dict):
        detail = result["detail"]
        if detail.get("status") == "awaiting_approval":
            return detail
    return result
