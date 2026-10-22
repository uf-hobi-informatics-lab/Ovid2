"""Unit tests for :class:`ovid2.mcp.client.WebApiClient`."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_base_url_strips_trailing_slash() -> None:
    """The client normalises its base URL by stripping the trailing slash."""
    from ovid2.mcp.client import WebApiClient

    client = WebApiClient("http://example.test/")
    try:
        assert client.base_url == "http://example.test"
    finally:
        # Best-effort — the underlying httpx client owns a pool.
        import asyncio

        asyncio.run(client.aclose())


@pytest.mark.unit
def test_get_returns_json_payload(web_app) -> None:
    """``get`` decodes ``application/json`` responses automatically."""
    pytest.importorskip("httpx")
    import anyio

    from ovid2.mcp.client import build_client_for_asgi

    client = build_client_for_asgi(web_app)

    async def _call() -> None:
        try:
            payload = await client.get("/api/v1/health")
            assert isinstance(payload, dict)
            assert "checks" in payload
            assert "worst" in payload
        finally:
            await client.aclose()

    anyio.run(_call)


@pytest.mark.unit
def test_get_raises_web_api_error_on_404(web_app) -> None:
    """4xx responses are surfaced as :class:`WebApiError`."""
    pytest.importorskip("httpx")
    import anyio

    from ovid2.mcp.client import WebApiError, build_client_for_asgi

    client = build_client_for_asgi(web_app)

    async def _call() -> None:
        try:
            with pytest.raises(WebApiError) as excinfo:
                await client.get("/api/v1/addresses/does_not_exist")
            assert excinfo.value.status_code == 404
            assert "not found" in excinfo.value.message.lower()
        finally:
            await client.aclose()

    anyio.run(_call)


@pytest.mark.unit
def test_get_text_returns_raw_body(web_app, isolated_xdg) -> None:
    """``get_text`` returns the raw ``text/plain`` body verbatim."""
    pytest.importorskip("httpx")
    import anyio

    from ovid2.mcp.client import WebApiError, build_client_for_asgi

    client = build_client_for_asgi(web_app)

    async def _call() -> None:
        try:
            # Unknown log path — expect a 404 raised as WebApiError.
            with pytest.raises(WebApiError):
                await client.get_text("/api/v1/jobs/does_not_exist/log")
        finally:
            await client.aclose()

    anyio.run(_call)


@pytest.mark.unit
def test_post_and_put_delete_round_trip(web_app, isolated_xdg) -> None:
    """``POST`` / ``PUT`` / ``DELETE`` cover the address CRUD flow."""
    pytest.importorskip("httpx")
    import anyio

    from ovid2.mcp.client import build_client_for_asgi

    client = build_client_for_asgi(web_app)

    async def _call() -> None:
        try:
            # Seed a disc connection first — addresses are W7-shaped now.
            await client.post(
                "/api/v1/connections/mcp_client_conn",
                json_body={"type": "disc", "args": {"host_path": "/tmp"}},
            )
            created = await client.post(
                "/api/v1/addresses/mcp_client_smoke",
                json_body={"connection": "mcp_client_conn", "location": {"path": "x"}},
            )
            assert created["action"] == "created"

            updated = await client.put(
                "/api/v1/addresses/mcp_client_smoke",
                json_body={"connection": "mcp_client_conn", "location": {"path": "y"}},
            )
            assert updated["action"] == "updated"

            # Address delete is gated on the approval flow now — the
            # first attempt returns an awaiting_approval envelope.
            gated = await client.delete("/api/v1/addresses/mcp_client_smoke")
            approval_id = gated["detail"]["approval_id"]
            await client.post(
                f"/api/v1/approvals/{approval_id}/approve",
                json_body={"decided_by": "test"},
            )
            deleted = await client.delete("/api/v1/addresses/mcp_client_smoke")
            assert deleted["action"] == "deleted"
        finally:
            await client.aclose()

    anyio.run(_call)
