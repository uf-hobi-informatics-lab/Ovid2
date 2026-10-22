"""``update_settings`` — persist the global runtime settings."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class UpdateSettingsArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`.

    Send the full settings model. Any omitted subtree falls back to
    the model default (i.e. sending ``{"defaults": {"engine": "x"}}``
    resets every other section to its default).
    """

    settings: dict[str, Any] = Field(
        ...,
        description="Full GlobalSettings JSON body.",
    )


TOOL_NAME = "update_settings"
TOOL_DESCRIPTION = (
    "Overwrite ``settings.json`` with the supplied model. Consumers "
    "pick up the change within one worker poll cycle."
)
ARGS_MODEL = UpdateSettingsArgs


async def run(client: WebApiClient, args: UpdateSettingsArgs) -> Any:
    """Call ``PUT /api/v1/settings``."""
    return await client.put("/api/v1/settings", json_body=args.settings)
