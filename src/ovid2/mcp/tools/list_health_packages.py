"""``list_health_packages`` — the EDC/DC report-cycle package catalogue.

Curated read-only wrapper over ``GET /api/v1/health/packages``. Lets an
LLM caller discover the available cycle-packages (e.g. ``20r1``) so it
can pass a valid id to :mod:`ovid2.mcp.tools.submit_health` via the
``packages`` argument. Each entry carries id / label / released /
description / checks, most-recent first (the first entry is the default
cycle).
"""

from __future__ import annotations

from typing import Any

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "list_health_packages"
TOOL_DESCRIPTION = (
    "List the available EDC / Data-Characterization report-cycle "
    "packages (e.g. 20r1). Each entry has id / label / released / "
    "description / checks, most-recent first. Use an id as the "
    "`packages` argument to submit_health to run every check in that "
    "cycle."
)
ARGS_MODEL = None


async def run(client: WebApiClient, args: None = None) -> Any:
    """Call ``GET /api/v1/health/packages``."""
    return await client.get("/api/v1/health/packages")
