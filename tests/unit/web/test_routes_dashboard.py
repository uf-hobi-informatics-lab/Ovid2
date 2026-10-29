"""Landing-page route tests using :class:`fastapi.testclient.TestClient`.

Regression-locks the console removal: ``/`` serves the card-grid
dashboard, the old ``/dashboard`` URL permanently redirects to ``/``,
the console surface (``/partials/console``, ``console.js``) is gone, and
no landing route leaks into the OpenAPI schema.
"""

from __future__ import annotations

import re

import pytest


@pytest.mark.unit
def test_root_serves_dashboard(web_client: object) -> None:
    """``GET /`` renders the dashboard shell with its HTMX body."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert 'id="dashboard-body"' in body
    assert 'hx-get="/partials/dashboard"' in body
    assert "Recent runs" in body
    # Console surface is gone.
    assert "console-cmd-input" not in body
    assert "/static/console.js" not in body
    assert "ovid2-palette-data" not in body


@pytest.mark.unit
def test_dashboard_is_active_nav_on_root(web_client: object) -> None:
    """The single Dashboard nav item is active on ``/``; no Console item."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    nav = re.search(r'<aside id="site-nav".*?</aside>', body, re.DOTALL)
    assert nav is not None
    assert 'href="/" class="nav-item active" data-nav="dashboard"' in nav.group(0)
    assert 'data-nav="console"' not in nav.group(0)
    assert ">Console<" not in nav.group(0)
    assert 'href="/dashboard"' not in nav.group(0)


@pytest.mark.unit
def test_legacy_dashboard_url_redirects(web_client: object) -> None:
    """``/dashboard`` permanently redirects to ``/``."""
    response = web_client.get("/dashboard", follow_redirects=False)  # type: ignore[attr-defined]
    assert response.status_code == 308
    assert response.headers["location"] == "/"


@pytest.mark.unit
def test_dashboard_partial_renders(web_client: object) -> None:
    """The HTMX polling partial still renders standalone."""
    response = web_client.get("/partials/dashboard")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "Recent runs" in response.text


@pytest.mark.unit
def test_console_surface_removed(web_client: object) -> None:
    """The console partial and its JS module no longer exist."""
    assert web_client.get("/partials/console").status_code == 404  # type: ignore[attr-defined]
    assert web_client.get("/static/console.js").status_code == 404  # type: ignore[attr-defined]


@pytest.mark.unit
def test_unmatched_page_marks_no_dashboard_active(web_client: object) -> None:
    """Pages outside the nav chain no longer fall back to an active Dashboard."""
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    assert 'href="/" class="nav-item active"' not in body


@pytest.mark.unit
def test_landing_routes_absent_from_openapi(web_client: object) -> None:
    """No dashboard HTML route leaks into the OpenAPI schema."""
    paths = set(web_client.get("/openapi.json").json()["paths"].keys())  # type: ignore[attr-defined]
    assert "/" not in paths
    assert "/dashboard" not in paths
    assert "/partials/dashboard" not in paths
