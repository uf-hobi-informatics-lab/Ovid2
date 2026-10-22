"""``test_connection`` — probe a connection with a cheap round-trip."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class TestConnectionArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the connection.")


TOOL_NAME = "test_connection"
TOOL_DESCRIPTION = (
    "Attempt a lightweight round-trip probe on the named connection "
    "(HEAD a bucket, SELECT 1 on a database, stat a directory, ...). "
    "Returns a ConnectionTestReport (ok, code, message, detail)."
)
ARGS_MODEL = TestConnectionArgs


async def run(client: WebApiClient, args: TestConnectionArgs) -> Any:
    """Call ``POST /api/v1/connections/{name}/test``."""
    return await client.post(f"/api/v1/connections/{args.name}/test", json_body={})
