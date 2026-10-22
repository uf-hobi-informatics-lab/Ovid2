"""Unit tests for the client-only command palette (Ctrl-K).

Covers the launcher layered on top of the v2 shell:

- The right tools rail renders the palette trigger + the modal mount.
- ``base.html`` loads ``cmdk.js`` (classic deferred script).
- ``cmdk.js`` stays a classic (non-module) script with no third-party
  imports and exposes ``window.Ovid2Palette``.
- The palette binds Ctrl/Cmd-K and derives its index from the drawer
  nav links (so it inherits RBAC gating for free).
- The feature is client-only: no palette route entered the schema.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_rail_has_palette_button(web_client: object) -> None:
    """The right tools rail hosts the palette trigger + modal mount."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'id="tool-palette"' in body
    assert 'id="command-palette"' in body


@pytest.mark.unit
def test_base_loads_cmdk_script(web_client: object) -> None:
    """base.html loads the palette script."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert "/static/cmdk.js" in body


@pytest.mark.unit
def test_cmdk_is_classic_script_without_imports(web_client: object) -> None:
    """``cmdk.js`` stays a classic script and defines its namespace."""
    body = web_client.get("/static/cmdk.js").text  # type: ignore[attr-defined]
    for line in body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import "), stripped
        assert not stripped.startswith("export "), stripped
    assert "window.Ovid2Palette" in body


@pytest.mark.unit
def test_cmdk_binds_ctrl_k(web_client: object) -> None:
    """The palette toggles on Ctrl/Cmd-K."""
    body = web_client.get("/static/cmdk.js").text  # type: ignore[attr-defined]
    assert "metaKey" in body
    assert "ctrlKey" in body
    assert '=== "k"' in body or '== "k"' in body


@pytest.mark.unit
def test_cmdk_indexes_nav_items(web_client: object) -> None:
    """The palette derives its index from the RBAC-gated nav links."""
    body = web_client.get("/static/cmdk.js").text  # type: ignore[attr-defined]
    assert "#site-nav a.nav-item" in body


@pytest.mark.unit
def test_cmdk_persists_recents_client_side(web_client: object) -> None:
    """Recent pages live in localStorage, not on the server."""
    body = web_client.get("/static/cmdk.js").text  # type: ignore[attr-defined]
    assert "ovid2RecentPages" in body


@pytest.mark.unit
def test_no_palette_route_in_schema(web_client: object) -> None:
    """The feature is client-only: no palette endpoint entered the schema."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = schema.get("paths", {})
    for path in paths:
        lowered = path.lower()
        assert "palette" not in lowered, f"unexpected palette route {path!r}"
        assert "cmdk" not in lowered, f"unexpected cmdk route {path!r}"
