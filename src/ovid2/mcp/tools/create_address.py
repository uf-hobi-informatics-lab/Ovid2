"""``create_address`` — create a new W7 (connection-shaped) address JSON config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class CreateAddressArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`.

    After the W7 connection-first flip, addresses are always views
    onto an existing :class:`Connection`. Credentials live on the
    connection; the address carries only the per-run ``location``
    (path / database / schema / bucket / container).
    """

    name: str = Field(..., description="Filename stem for the new address config.")
    connection: str = Field(
        ...,
        description=(
            "Name of an existing connection this address will view. "
            "Use list_connections / create_connection first if there "
            "is no suitable one."
        ),
    )
    location: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-address location payload (path, database, schema, "
            "bucket, container, …). Fields required depend on the "
            "connection type — see get_connection_types."
        ),
    )


TOOL_NAME = "create_address"
TOOL_DESCRIPTION = (
    "Create a new address JSON config in the user-scoped configs "
    "directory. Addresses are views onto connections: pass the name "
    "of an existing connection plus a small location payload. Fails "
    "with HTTP 400 when a config of the same name already exists or "
    "when the referenced connection is missing. Use update_address "
    "for idempotent create-or-update."
)
ARGS_MODEL = CreateAddressArgs


async def run(client: WebApiClient, args: CreateAddressArgs) -> Any:
    """Call ``POST /api/v1/addresses/{name}``."""
    return await client.post(
        f"/api/v1/addresses/{args.name}",
        json_body={"connection": args.connection, "location": args.location},
    )
