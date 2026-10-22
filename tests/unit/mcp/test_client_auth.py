"""Unit tests for service-principal bearer-token wiring in the MCP client.

Covers todo 21's MCP-side acceptance criteria:

- A ``token`` passed to :class:`ovid2.mcp.client.WebApiClient` is attached
  as ``Authorization: Bearer <token>`` on the underlying ``httpx`` client,
  so every request inherits it.
- With no token, no ``Authorization`` header is set (today's behaviour).
"""

from __future__ import annotations

import asyncio

import pytest


def _close(client: object) -> None:
    """Best-effort close of the client's underlying httpx pool.

    Uses a fresh event loop so the helper is robust to test ordering
    (Python 3.10 raises from ``asyncio.get_event_loop()`` when a prior
    test has already closed the thread's default loop).
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.aclose())  # type: ignore[attr-defined]
    finally:
        loop.close()


@pytest.mark.unit
def test_token_sets_bearer_header() -> None:
    """A configured token becomes a Bearer Authorization header on every call."""
    from ovid2.mcp.client import WebApiClient

    client = WebApiClient("http://example.test", token="s3cr3t-token")
    try:
        assert client._client.headers["authorization"] == "Bearer s3cr3t-token"
    finally:
        _close(client)


@pytest.mark.unit
def test_no_token_sends_no_auth_header() -> None:
    """Without a token, the client sets no Authorization header."""
    from ovid2.mcp.client import WebApiClient

    client = WebApiClient("http://example.test", token=None)
    try:
        assert "authorization" not in client._client.headers
    finally:
        _close(client)


@pytest.mark.unit
def test_default_token_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ctor default reads ``MCP_API_TOKEN`` from mcp.settings.

    ``OVID2_MCP_API_TOKEN`` is read at import time into
    ``mcp.settings.MCP_API_TOKEN`` and bound as the ctor default, so we
    reload both modules under a patched environment to observe it.
    """
    import importlib

    monkeypatch.setenv("OVID2_MCP_API_TOKEN", "from-settings")
    import ovid2.mcp.settings as settings_mod

    importlib.reload(settings_mod)
    assert settings_mod.MCP_API_TOKEN == "from-settings"

    import ovid2.mcp.client as client_mod

    importlib.reload(client_mod)
    client = client_mod.WebApiClient("http://example.test")
    try:
        assert client._client.headers["authorization"] == "Bearer from-settings"
    finally:
        _close(client)
        importlib.reload(settings_mod)
        importlib.reload(client_mod)


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
