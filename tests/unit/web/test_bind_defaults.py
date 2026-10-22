"""Unit tests for web-tier network bind defaults.

The web and MCP services bind to loopback only by default; TLS and public
exposure are delegated to the shared host reverse proxy. See
the 2026-08-24-web-tls-via-shared-reverse-proxy design note.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.unit
def test_bind_defaults_are_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB_BIND/MCP_BIND default to loopback and MCP_UPSTREAM follows WEB_BIND."""
    monkeypatch.delenv("OVID2_WEB_BIND", raising=False)
    monkeypatch.delenv("OVID2_MCP_BIND", raising=False)
    monkeypatch.delenv("OVID2_WEB_URL", raising=False)

    import ovid2.web.settings as web_settings

    reloaded = importlib.reload(web_settings)
    try:
        assert reloaded.WEB_BIND == "127.0.0.1:8090"
        assert reloaded.MCP_BIND == "127.0.0.1:8091"
        assert reloaded.MCP_UPSTREAM == "http://127.0.0.1:8090"
        # Must never default to the LAN interface (would bypass the proxy).
        assert not reloaded.WEB_BIND.startswith("0.0.0.0")
        assert not reloaded.MCP_BIND.startswith("0.0.0.0")
    finally:
        # Restore module state for any import-order-sensitive tests.
        importlib.reload(reloaded)


@pytest.mark.unit
def test_bind_env_override_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can still override the bind via env (e.g. dev/tunnel)."""
    monkeypatch.setenv("OVID2_WEB_BIND", "0.0.0.0:9999")

    import ovid2.web.settings as web_settings

    reloaded = importlib.reload(web_settings)
    try:
        assert reloaded.WEB_BIND == "0.0.0.0:9999"
        assert reloaded.MCP_UPSTREAM == "http://0.0.0.0:9999"
    finally:
        monkeypatch.delenv("OVID2_WEB_BIND", raising=False)
        importlib.reload(reloaded)
