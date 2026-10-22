"""MCP server — bridges the ``ovid2 web`` REST API into MCP tool calls.

The server is intentionally tiny: it discovers tools via
:func:`ovid2.mcp.tools.discover_tools`, derives each tool's JSON-schema
from its Pydantic ``ARGS_MODEL``, and dispatches ``tools/call`` to the
tool's ``run()`` coroutine. All I/O to the upstream FastAPI backend
goes through one shared :class:`ovid2.mcp.client.WebApiClient`.

Two transports are supported:

* ``stdio`` — the default; drives the server via
  :func:`mcp.stdio_server`. Suitable for LLM clients that spawn MCP
  processes on demand (Claude Desktop, cursor.sh, opencode …).
* ``http`` — Streamable HTTP transport exposed by the MCP SDK.
  Suitable for long-running server deployments; see the
  ``ovid2-mcp.service`` systemd unit template.

The MCP surface never imports pipeline packages — every side effect
goes through HTTP against the FastAPI backend. This keeps the MCP
tier install-light (``pip install ovid2[mcp]`` pulls the MCP SDK plus
``httpx`` only) and enforces the same "web is a client of the
pipeline" boundary the contract tests enforce for the web tier.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import stdio_server, types
from mcp.server import Server
from pydantic import BaseModel, ValidationError

from ovid2 import __version__
from ovid2.mcp.client import WebApiClient, WebApiError
from ovid2.mcp.settings import (
    MCP_SERVER_DESCRIPTION,
    MCP_SERVER_NAME,
    MCP_SERVER_TITLE,
    MCP_UPSTREAM,
)
from ovid2.mcp.tools import ToolSpec, discover_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON-schema helpers
# ---------------------------------------------------------------------------


_EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _tool_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """Derive an MCP tool input schema from a :class:`ToolSpec`.

    Pydantic 2's ``model_json_schema`` already emits JSON-Schema
    Draft-2020-12; the MCP protocol accepts any valid JSON Schema
    object here, so we pass it through verbatim.
    """
    if spec.args_model is None:
        return _EMPTY_SCHEMA
    schema = spec.args_model.model_json_schema()
    # Some MCP clients reject ``$defs`` at the top level; keep the schema
    # simple and shallow. Pydantic emits ``$defs`` only for nested models,
    # and none of our tools nest. We still tolerate it defensively.
    return schema


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@dataclass
class _Registry:
    """In-memory tool registry the handlers close over."""

    tools: dict[str, ToolSpec]
    client: WebApiClient

    def list_result(self) -> types.ListToolsResult:
        """Return the payload for ``tools/list``."""
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    input_schema=_tool_input_schema(spec),
                )
                for spec in sorted(self.tools.values(), key=lambda s: s.name)
            ]
        )


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    """Wrap ``payload`` in a :class:`types.CallToolResult`.

    The payload is JSON-serialised so agents receive a deterministic
    shape regardless of whether the tool returned a dict, list, or
    scalar. Text-only tools (e.g. ``get_job_log``) already return a
    ``str`` and are passed through unchanged.
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    content = types.TextContent(type="text", text=text)
    return types.CallToolResult(content=[content], is_error=is_error)


async def _handle_call(registry: _Registry, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Dispatch one ``tools/call`` request."""
    spec = registry.tools.get(params.name)
    if spec is None:
        return _text_result(
            {"error": "unknown_tool", "name": params.name},
            is_error=True,
        )

    raw_args = params.arguments or {}
    args: BaseModel | None
    if spec.args_model is None:
        args = None
    else:
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            return _text_result(
                {"error": "invalid_arguments", "detail": exc.errors()},
                is_error=True,
            )

    try:
        result = await spec.run(registry.client, args)
    except WebApiError as exc:
        return _text_result(
            {"error": "upstream_error", "status": exc.status_code, "detail": exc.message},
            is_error=True,
        )
    except Exception as exc:
        logger.exception("Tool %s raised", spec.name)
        return _text_result(
            {"error": "internal_error", "detail": str(exc)},
            is_error=True,
        )
    return _text_result(result)


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


@dataclass
class ServerBundle:
    """Handle returned by :func:`build_server`.

    The bundle owns the :class:`mcp.server.Server`, the shared
    :class:`WebApiClient`, and the tool registry. Callers should
    ``await bundle.aclose()`` at shutdown to release the HTTP pool.
    """

    server: Server[Any]
    client: WebApiClient
    tools: dict[str, ToolSpec]

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self.client.aclose()


def build_server(
    *,
    upstream: str = MCP_UPSTREAM,
    client: WebApiClient | None = None,
    tools: list[ToolSpec] | None = None,
) -> ServerBundle:
    """Construct an MCP server bound to the given upstream / client.

    Args:
        upstream: Base URL of the FastAPI backend. Ignored when
            ``client`` is supplied.
        client: Pre-built :class:`WebApiClient` (used by tests to
            inject an in-process ASGI transport).
        tools: Explicit tool list, primarily for tests. Defaults to
            :func:`discover_tools`.
    """
    resolved_tools = tools if tools is not None else discover_tools()
    tool_map = {spec.name: spec for spec in resolved_tools}
    api_client = client if client is not None else WebApiClient(upstream)
    registry = _Registry(tools=tool_map, client=api_client)

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return registry.list_result()

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await _handle_call(registry, params)

    server: Server[Any] = Server(
        MCP_SERVER_NAME,
        version=__version__,
        title=MCP_SERVER_TITLE,
        description=MCP_SERVER_DESCRIPTION,
        instructions=(
            "Every tool wraps a versioned REST endpoint under "
            "/api/v1/*. Read-oriented tools return decoded JSON; "
            "write-oriented tools return the same WriteResult / "
            "detail shape the REST API emits. On upstream error, "
            "results carry is_error=True with {error, status, "
            "detail}."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    return ServerBundle(server=server, client=api_client, tools=tool_map)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _bundle_lifespan(bundle: ServerBundle) -> AsyncIterator[ServerBundle]:
    """Close the HTTP client on shutdown."""
    try:
        yield bundle
    finally:
        await bundle.aclose()


async def run_stdio(upstream: str = MCP_UPSTREAM) -> int:
    """Run the MCP server on stdio until the client disconnects.

    Returns the process exit code (always ``0`` on graceful shutdown).
    """
    bundle = build_server(upstream=upstream)
    async with _bundle_lifespan(bundle), stdio_server() as (read_stream, write_stream):
        await bundle.server.run(
            read_stream,
            write_stream,
            bundle.server.create_initialization_options(),
        )
    return 0


async def run_http(bind: str, upstream: str = MCP_UPSTREAM) -> int:
    """Run the MCP server on the Streamable HTTP transport.

    The bind string is ``host:port`` (matching the ``ovid2 web`` bind
    format). The transport is served by uvicorn under the hood; see
    :meth:`mcp.server.Server.streamable_http_app`.
    """
    import uvicorn  # local import — the ``mcp`` extra already pulls it in

    host, _, port = bind.partition(":")
    port_int = int(port) if port else 8091

    bundle = build_server(upstream=upstream)
    app = bundle.server.streamable_http_app()
    config = uvicorn.Config(
        app,
        host=host or "127.0.0.1",
        port=port_int,
        log_level="info",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await bundle.aclose()
    return 0
