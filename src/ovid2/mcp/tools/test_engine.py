"""``test_engine`` — probe an engine's session build."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class TestEngineArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="Filename stem of the engine.")


TOOL_NAME = "test_engine"
TOOL_DESCRIPTION = (
    "Probe the named engine by shelling out to `ovid2 tools "
    "test-engine <name>`. The subprocess builds a session, runs "
    "SELECT 1, and tears the session down. Returns an "
    "EngineTestReport (ok, code, message, detail)."
)
ARGS_MODEL = TestEngineArgs


async def run(client: WebApiClient, args: TestEngineArgs) -> Any:
    """Call ``POST /api/v1/engines/{name}/test``."""
    return await client.post(f"/api/v1/engines/{args.name}/test", json_body={})
