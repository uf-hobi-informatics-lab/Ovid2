"""``update_address`` — idempotent create-or-update of an address config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class UpdateAddressArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`.

    Idempotent PUT — accepts either shape so that both **new**
    (W7 connection-shaped) addresses and **legacy** (pre-W7,
    embedded-credentials) addresses can be edited through the same
    tool. When ``connection`` is set, ``location`` is used and the
    ``src_*`` fields are ignored. Otherwise the legacy shape applies.

    Creation of brand-new legacy-shape addresses is refused server-
    side (HTTP 400); use ``create_address`` with a connection instead.
    """

    name: str = Field(..., description="Filename stem of the address config.")
    connection: str | None = Field(
        None,
        description=(
            "W7 shape: name of the backing connection. When set, "
            "``location`` (non-secret path/database/etc.) is used and "
            "``src_*`` fields are ignored."
        ),
    )
    location: dict[str, Any] | None = Field(
        None,
        description="W7 shape: per-address location payload.",
    )
    src_name: str | None = Field(
        None,
        description="Legacy shape: address type — 'disc' or 'snowflake'.",
    )
    src_args: dict[str, Any] | None = Field(
        None,
        description=(
            "Legacy shape: address arguments. Sensitive keys sent as "
            "'***' preserve the on-disk value; real strings replace it."
        ),
    )


TOOL_NAME = "update_address"
TOOL_DESCRIPTION = (
    "Idempotent create-or-update of an address JSON config. Prefer "
    "the W7 shape (connection + location); the legacy (src_name + "
    "src_args) shape is accepted only when the address already "
    "exists on disk in that shape."
)
ARGS_MODEL = UpdateAddressArgs


async def run(client: WebApiClient, args: UpdateAddressArgs) -> Any:
    """Call ``PUT /api/v1/addresses/{name}``.

    Body carries whichever shape the caller populated; the service
    validates that exactly one shape is present.
    """
    body: dict[str, Any] = {}
    if args.connection is not None:
        body["connection"] = args.connection
        body["location"] = args.location or {}
    else:
        if args.src_name is not None:
            body["src_name"] = args.src_name
        if args.src_args is not None:
            body["src_args"] = args.src_args
    return await client.put(
        f"/api/v1/addresses/{args.name}",
        json_body=body,
    )
