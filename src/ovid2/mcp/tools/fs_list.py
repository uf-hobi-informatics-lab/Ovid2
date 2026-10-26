"""``fs_list`` — list one directory on the host filesystem."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient

TOOL_NAME = "fs_list"
TOOL_DESCRIPTION = (
    "List the contents of one directory on the host filesystem (the "
    "same view the web-tier user sees). Scoped by the "
    "``settings.fs_picker.roots`` allowlist. Returns an FsListing with "
    "``path``, ``parent`` (or None), and ``entries`` (each with "
    "``name``, ``type``, ``is_symlink``). Read-only: no side effects. "
    "Rejects paths outside the allowlist with HTTP 403."
)


class Args(BaseModel):
    """Arguments for :data:`TOOL_NAME`."""

    path: str | None = Field(
        default=None,
        description=(
            "Absolute host path to list. When omitted, the first entry "
            "of ``settings.fs_picker.roots`` is used (``/`` by default)."
        ),
    )
    show_hidden: bool | None = Field(
        default=None,
        description=(
            "Include dot-entries. Falls back to ``settings.fs_picker.show_hidden`` when omitted."
        ),
    )


ARGS_MODEL = Args


async def run(client: WebApiClient, args: Args) -> Any:
    """Call ``GET /api/v1/fs/list``."""
    kwargs: dict[str, Any] = {}
    if args.path is not None:
        kwargs["path"] = args.path
    if args.show_hidden is not None:
        kwargs["show_hidden"] = str(args.show_hidden).lower()
    return await client.get("/api/v1/fs/list", **kwargs)
