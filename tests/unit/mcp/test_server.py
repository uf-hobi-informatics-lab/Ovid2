"""End-to-end unit tests for the MCP server dispatcher.

These tests exercise the full ``tools/list`` and ``tools/call`` code
paths against an in-process FastAPI app (via
:func:`ovid2.mcp.client.build_client_for_asgi`). They do **not** speak
the JSON-RPC wire protocol — that is the SDK's responsibility.
"""

from __future__ import annotations

import json

import pytest


@pytest.mark.unit
def test_list_result_contains_every_tool(mcp_bundle) -> None:
    """``list_result`` returns a :class:`ListToolsResult` with the full set."""
    from ovid2.mcp.server import _Registry

    registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
    result = registry.list_result()
    assert len(result.tools) == len(mcp_bundle.tools)

    # Every tool has a non-empty description and a JSON-Schema object.
    for tool in result.tools:
        assert tool.description
        schema = tool.input_schema
        assert isinstance(schema, dict)
        assert schema["type"] == "object"


@pytest.mark.unit
def test_parameterless_tools_expose_empty_schema(mcp_bundle) -> None:
    """Tools with ``ARGS_MODEL=None`` render as an empty object schema."""
    from ovid2.mcp.server import _Registry

    registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
    result = registry.list_result()
    empty = [t for t in result.tools if not t.input_schema.get("properties")]
    assert {t.name for t in empty} >= {
        "health",
        "list_addresses",
        "list_pipelines",
        "list_secrets",
        "install_status",
        "install_bootstrap",
    }


@pytest.mark.unit
def test_call_health_returns_text_content(mcp_bundle) -> None:
    """``call_tool('health', {})`` returns a JSON-encoded TextContent block."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        params = types.CallToolRequestParams(name="health", arguments={})
        result = await _handle_call(registry, params)
        assert result.is_error is False
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        payload = json.loads(result.content[0].text)
        assert "worst" in payload and "checks" in payload

    anyio.run(_call)


@pytest.mark.unit
def test_call_unknown_tool_is_error(mcp_bundle) -> None:
    """Unknown names come back as ``is_error=True`` with ``error='unknown_tool'``."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        params = types.CallToolRequestParams(name="does_not_exist", arguments={})
        result = await _handle_call(registry, params)
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "unknown_tool"
        assert payload["name"] == "does_not_exist"

    anyio.run(_call)


@pytest.mark.unit
def test_call_with_invalid_args_is_error(mcp_bundle) -> None:
    """A ValidationError yields ``error='invalid_arguments'`` with details."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        # get_address requires "name" — send an empty dict.
        params = types.CallToolRequestParams(name="get_address", arguments={})
        result = await _handle_call(registry, params)
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "invalid_arguments"
        assert isinstance(payload["detail"], list)

    anyio.run(_call)


@pytest.mark.unit
def test_call_upstream_404_is_reported(mcp_bundle) -> None:
    """Upstream 4xx flows through as ``error='upstream_error'`` with status."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        params = types.CallToolRequestParams(
            name="get_address", arguments={"name": "does_not_exist"}
        )
        result = await _handle_call(registry, params)
        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "upstream_error"
        assert payload["status"] == 404

    anyio.run(_call)


@pytest.mark.unit
def test_address_crud_round_trip(mcp_bundle) -> None:
    """create_address → update_address → delete_address flow works end-to-end.

    After the W7 flip, ``create_address`` requires a backing
    connection. Set one up first via the REST API, then exercise the
    tool trio.
    """
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)

        # Seed a disc connection so create_address has something to view.
        await mcp_bundle.client.post(
            "/api/v1/connections/mcp_conn",
            json_body={"type": "disc", "args": {"host_path": "/tmp"}},
        )

        create_params = types.CallToolRequestParams(
            name="create_address",
            arguments={
                "name": "mcp_round_trip",
                "connection": "mcp_conn",
                "location": {"path": "a"},
            },
        )
        result = await _handle_call(registry, create_params)
        assert result.is_error is False, result.content[0].text
        assert json.loads(result.content[0].text)["action"] == "created"

        update_params = types.CallToolRequestParams(
            name="update_address",
            arguments={
                "name": "mcp_round_trip",
                "connection": "mcp_conn",
                "location": {"path": "b"},
            },
        )
        result = await _handle_call(registry, update_params)
        assert json.loads(result.content[0].text)["action"] == "updated"

        delete_params = types.CallToolRequestParams(
            name="delete_address",
            arguments={"name": "mcp_round_trip"},
        )
        # Address delete is gated: the first tool call returns an
        # awaiting_approval envelope. A human approves out-of-band, then
        # the retry deletes.
        result = await _handle_call(registry, delete_params)
        gated = json.loads(result.content[0].text)
        approval_id = gated["detail"]["approval_id"]
        await mcp_bundle.client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            json_body={"decided_by": "test"},
        )
        result = await _handle_call(registry, delete_params)
        assert json.loads(result.content[0].text)["action"] == "deleted"

    anyio.run(_call)


@pytest.mark.unit
def test_list_addresses_reflects_disk(mcp_bundle, sample_address) -> None:
    """The tool sees address configs freshly dropped on disk."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        params = types.CallToolRequestParams(name="list_addresses", arguments={})
        result = await _handle_call(registry, params)
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert any(entry["name"] == "test_disc" for entry in payload)

    anyio.run(_call)


@pytest.mark.unit
def test_install_status_returns_step_list(mcp_bundle) -> None:
    """The wizard status tool returns the ordered step list."""
    import anyio
    from mcp import types

    from ovid2.mcp.server import _handle_call, _Registry

    async def _call() -> None:
        registry = _Registry(tools=mcp_bundle.tools, client=mcp_bundle.client)
        params = types.CallToolRequestParams(name="install_status", arguments={})
        result = await _handle_call(registry, params)
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert "steps" in payload
        keys = [s["key"] for s in payload["steps"]]
        assert "bootstrap" in keys
        assert "done" in keys


    anyio.run(_call)


@pytest.mark.unit
def test_server_bundle_can_construct_initialization_options(mcp_bundle) -> None:
    """The bundled :class:`Server` yields valid initialization options."""
    opts = mcp_bundle.server.create_initialization_options()
    assert opts.server_name == "ovid2-mcp"
    # Non-empty capability envelope
    assert opts.capabilities is not None
