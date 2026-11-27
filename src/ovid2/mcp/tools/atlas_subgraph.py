"""``atlas_subgraph`` — the Atlas around selected nodes (row-free)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class AtlasSubgraphArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    ids: list[str] = Field(
        ...,
        description="Node ids: config:<name>, package:<name>, engine:<name>, address:<name>, …",
        min_length=1,
        max_length=50,
    )
    depth: int = Field(1, ge=0, le=2, description="Neighbour hops to include.")


TOOL_NAME = "atlas_subgraph"
TOOL_DESCRIPTION = (
    "Read the Ovid Atlas around some nodes: each node's layer (storage, compute, "
    "algorithm), kind and metadata plus the beams that link them (reads, writes, "
    "runs_on, implements, holds, step, flows). Static and row-free: no row values, "
    "no secrets. Read-only."
)
ARGS_MODEL = AtlasSubgraphArgs


async def run(client: WebApiClient, args: AtlasSubgraphArgs) -> Any:
    """Call ``GET /api/v1/atlas/subgraph``."""
    return await client.get("/api/v1/atlas/subgraph", ids=",".join(args.ids), depth=args.depth)
