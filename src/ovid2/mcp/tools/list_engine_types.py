"""``list_engine_types`` — return the engine field-spec catalogue."""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_engine_types"
TOOL_DESCRIPTION = (
    "Return the registered engine types (pyspark, databricks_spark, "
    "snowpark) and their field specs. Consumers use this to build the "
    "dynamic new-engine form and to validate ``create_engine`` args "
    "client-side."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/engines/types``."""
    return await client.get("/api/v1/engines/types")
