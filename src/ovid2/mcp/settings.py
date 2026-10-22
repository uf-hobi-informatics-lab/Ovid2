"""MCP server settings.

The MCP server is a thin adapter over the FastAPI REST API. It has two
knobs that matter:

- ``OVID2_WEB_URL`` — the upstream base URL of ``ovid2 web`` (default:
  derived from :data:`ovid2.web.settings.WEB_BIND`).
- ``OVID2_MCP_BIND`` — the ``host:port`` the HTTP transport binds to
  when ``ovid2 mcp --transport http`` is used (default:
  ``127.0.0.1:8091``, loopback only; fronted by the shared host reverse
  proxy on a distinct hostname).

Everything else — request timeout, tool-list cache TTL — is a small
constant with an environment override, following the same pattern as
:mod:`ovid2.web.settings`.
"""

from __future__ import annotations

import os

# Re-export the two variables that live on the web-tier settings so callers
# never have to reach into ``ovid2.web`` themselves.
from ovid2.web.settings import MCP_BIND, MCP_UPSTREAM

__all__ = [
    "MCP_ACTOR",
    "MCP_API_TOKEN",
    "MCP_BIND",
    "MCP_HTTP_TIMEOUT",
    "MCP_SERVER_DESCRIPTION",
    "MCP_SERVER_NAME",
    "MCP_SERVER_TITLE",
    "MCP_UPSTREAM",
]

#: Timeout applied to every upstream HTTP request (seconds). Overridable
#: through ``OVID2_MCP_HTTP_TIMEOUT``.
MCP_HTTP_TIMEOUT: float = float(os.environ.get("OVID2_MCP_HTTP_TIMEOUT", "30.0"))

#: MCP server identity — surfaced to clients during initialization.
MCP_SERVER_NAME: str = "ovid2-mcp"

MCP_SERVER_TITLE: str = "Ovid2 control panel (MCP)"

MCP_SERVER_DESCRIPTION: str = (
    "Model-Context-Protocol server that fronts the Ovid2 web control "
    "panel. Every tool wraps a versioned REST endpoint under "
    "/api/v1/*; see ovid2.web.models for the response schemas."
)

#: ``submitted_by`` value the MCP server passes to write endpoints so
#: audit rows are distinguishable from browser sessions.
MCP_ACTOR: str = os.environ.get("OVID2_MCP_ACTOR", "mcp")

#: Optional service-principal bearer token. When set (via
#: ``OVID2_MCP_API_TOKEN``) the client sends ``Authorization: Bearer
#: <token>`` on every upstream request; the web tier maps the token to a
#: fixed role (see :mod:`ovid2.web.auth.service_token`). Left ``None`` the
#: client sends no auth header — matching the network-perimeter-only
#: default. The token is a plaintext credential and is never logged.
MCP_API_TOKEN: str | None = os.environ.get("OVID2_MCP_API_TOKEN")
