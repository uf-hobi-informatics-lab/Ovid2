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
    "see data). Response shape: OdsTableSample { path, columns: [str], "
    "rows: [[value, …]], truncated, limit, supported, message }."
)
ARGS_MODEL = OdsSampleTableArgs


async def run(client: WebApiClient, args: OdsSampleTableArgs) -> Any:
    """Call ``GET /api/v1/dataspace/table/sample?path=…&limit=…``."""
    return await client.get("/api/v1/dataspace/table/sample", path=args.path, limit=args.limit)
