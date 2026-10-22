"""``test_llm`` — probe one LLM connection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class TestLlmArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    name: str = Field(..., description="LLM connection name.")


TOOL_NAME = "test_llm"
TOOL_DESCRIPTION = (
    "Probe an LLM connection: authenticate against the provider and "
    "confirm the configured model is available. Runs in an executor "
    "subprocess; returns an LlmTestReport (ok, code, message, detail)."
)
ARGS_MODEL = TestLlmArgs


async def run(client: WebApiClient, args: TestLlmArgs) -> Any:
    """Call ``POST /api/v1/llms/{name}/test``."""
    return await client.post(f"/api/v1/llms/{args.name}/test", json_body={})
