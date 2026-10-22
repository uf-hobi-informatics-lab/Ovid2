"""Ovid2 MCP server package.

The MCP (Model-Context-Protocol) server exposes the ``ovid2 web`` REST
API as a set of tools any LLM/agent can call. See
the adr-web-control-panel design note for the design.

The ``ovid2 mcp`` CLI subcommand is registered by
:mod:`ovid2.mcp.cli`; the server implementation lives in
:mod:`ovid2.mcp.server`; individual tools live in
:mod:`ovid2.mcp.tools`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
