"""Tests for the sidebar layout, theme toggle, and branding route.

Updated for the web-panel v2 shell:

- Sidebar is called ``drawer`` (semantic ``<aside class="drawer">``).
- Brand mark is served as SVG (``/branding/ovid_logo.svg``) so the
  browser can never render it at intrinsic pixel size.
- Nav items use the ``nav-item`` class plus a conditional ``active``
  class.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_branding_svg_served(web_client: object) -> None:
    """``GET /branding/ovid_logo.svg`` returns the packaged SVG."""
    response = web_client.get("/branding/ovid_logo.svg")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers.get("cache-control") == "public, max-age=300"
    assert response.headers.get("etag")
    assert response.content.startswith(b"<?xml") or response.content.startswith(b"<svg")


@pytest.mark.unit
def test_branding_logo_served(web_client: object) -> None:
    """``GET /branding/ovid_logo.png`` still serves the master PNG."""
    response = web_client.get("/branding/ovid_logo.png")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers.get("cache-control") == "public, max-age=300"
    # PNG signature: 89 50 4E 47 0D 0A 1A 0A
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(response.content) > 1000


@pytest.mark.unit
def test_branding_small_variants_served(web_client: object) -> None:
    """Pre-rendered small PNGs (32/64/128) are served with sane byte counts."""
    for size in (32, 64, 128):
        response = web_client.get(f"/branding/ovid_logo_{size}.png")  # type: ignore[attr-defined]
        assert response.status_code == 200, f"missing /branding/ovid_logo_{size}.png"
        assert response.headers["content-type"] == "image/png"
        # A 32-px PNG must be much smaller than the 861 KB master.
        assert len(response.content) < 50_000


@pytest.mark.unit
def test_branding_favicon_png_served(web_client: object) -> None:
    """``GET /branding/favicon.png`` serves the small 32 px PNG."""
    response = web_client.get("/branding/favicon.png")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    # Now aliased to the 32-px variant, so it's tiny.
    assert len(response.content) < 5_000


@pytest.mark.unit
def test_branding_favicon_svg_served(web_client: object) -> None:
    """``GET /branding/favicon.svg`` serves the SVG-wrapped mark."""
    response = web_client.get("/branding/favicon.svg")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"


@pytest.mark.unit
def test_branding_etag_conditional(web_client: object) -> None:
    """Serving an asset with a matching ``If-None-Match`` yields 304."""
    response = web_client.get("/branding/ovid_logo.svg")  # type: ignore[attr-defined]
    etag = response.headers["etag"]
    cached = web_client.get(  # type: ignore[attr-defined]
        "/branding/ovid_logo.svg", headers={"If-None-Match": etag}
    )
    assert cached.status_code == 304


@pytest.mark.unit
def test_branding_unknown_asset_404(web_client: object) -> None:
    """Filenames outside the allow-list return 404."""
    response = web_client.get("/branding/does-not-exist.png")  # type: ignore[attr-defined]
    assert response.status_code == 404


@pytest.mark.unit
def test_branding_route_excluded_from_openapi(web_client: object) -> None:
    """The branding route is a static-style GET; keep it out of the schema."""
    response = web_client.get("/openapi.json")  # type: ignore[attr-defined]
    assert response.status_code == 200
    paths = response.json().get("paths", {})
    assert "/branding/{filename}" not in paths


@pytest.mark.unit
def test_dashboard_has_drawer_and_theme_toggle(web_client: object) -> None:
    """The dashboard renders the v2 drawer layout with the Appearance tool."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    # New chrome elements.
    assert 'class="drawer"' in body
    # The day/night toggle was replaced by the Appearance tool rail button.
    assert 'id="tool-appearance"' in body
    assert 'id="drawer-toggle"' in body
    # SVG favicon link present (with PNG fallback for older browsers).
    assert '<link rel="icon"' in body
    assert "/branding/favicon.svg" in body
    # Topbar wordmark: the "Ovid2" text is the home link, styled with the
    # theme display face. The planet/orbit glyph was removed permanently.
    assert 'class="brand-mark-link" href="/"' in body
    assert '<span class="brand-text">Ovid<span class="brand-accent">2</span>' in body
    assert 'class="brand-mark"' not in body
    assert 'class="brand-logo"' not in body
    # Static assets referenced.
    assert "/static/theme.js" in body
    assert "/static/nav.js" in body
    # Drawer section labels.
    assert 'class="side-group"' in body
    assert "Operate" in body
    assert "Configure" in body


@pytest.mark.unit
def test_dashboard_marks_active_nav(web_client: object) -> None:
    """The dashboard route is marked ``active`` in the drawer."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    # First drawer nav-item (Dashboard) should carry the active class.
    assert 'data-nav="dashboard"' in body
    assert "nav-item active" in body


@pytest.mark.unit
def test_pipelines_page_marks_active_nav(web_client: object) -> None:
    """Visiting ``/pipelines`` marks the Pipelines link active."""
    response = web_client.get("/pipelines")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    # Find the Pipelines nav-item and confirm it carries the active class.
    # We look for the substring pattern that only appears when Pipelines
    # is the active page.
    assert 'href="/pipelines" class="nav-item active"' in body
    # Dashboard link must not carry the active class on the pipelines page.
    assert 'href="/" class="nav-item active"' not in body


@pytest.mark.unit
def test_health_page_marks_active_nav(web_client: object) -> None:
    """Visiting ``/health`` marks the Health link active."""
    response = web_client.get("/health")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert 'href="/health" class="nav-item active"' in body


@pytest.mark.unit
def test_theme_js_served(web_client: object) -> None:
    """``theme.js`` is served from ``/static`` and applies ``data-theme``."""
    response = web_client.get("/static/theme.js")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert "OVID2_APPEARANCE" in body
    assert "data-theme" in body


@pytest.mark.unit
def test_nav_js_served(web_client: object) -> None:
    """``nav.js`` is served from ``/static`` and wires the drawer toggle."""
    response = web_client.get("/static/nav.js")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert "drawer-toggle" in body
    assert "drawer-open" in body


@pytest.mark.unit
def test_base_template_has_prepaint_script(web_client: object) -> None:
    """The pre-paint theme setter is inlined in ``<head>`` before the CSS."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    # Inline script sets data-theme before stylesheet loads.
    prepaint_pos = body.find("window.OVID2_APPEARANCE")
    css_pos = body.find("/static/ovid2.css")
    assert prepaint_pos != -1
    assert css_pos != -1
    assert prepaint_pos < css_pos, "pre-paint theme setter must run before CSS load"
