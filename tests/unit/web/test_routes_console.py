"""Console landing-page route tests using :class:`fastapi.testclient.TestClient`.

Regression-locks the ``/`` console surface (Todo 8 of the
console-landing-page plan): the terminal shell + palette data at ``/``,
the HTMX panel partial at ``/partials/console``, the preserved dashboard
at ``/dashboard``, and the OpenAPI-schema invariant (no new documented
paths).
"""

from __future__ import annotations

import json
import re

import pytest


@pytest.mark.unit
def test_console_landing_renders(web_client: object) -> None:
    """``GET /`` serves the console shell with palette + cursor + panels JS."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert 'id="console-body"' in body
    assert 'id="ovid2-palette-data"' in body
    assert 'class="console-cmd-input"' in body
    assert 'class="console-cursor"' in body
    assert "/static/console.js" in body
    # The faded centre logo was removed in the terminal-log restyle.
    # (The apple-touch-icon in base.html still references ovid_logo_128.png,
    # so key the lock on the console-specific class, not the filename.)
    assert "console-bg" not in body


@pytest.mark.unit
def test_console_palette_data_has_submit(web_client: object) -> None:
    """The embedded palette JSON parses and includes the /submit target."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    match = re.search(
        r'<script id="ovid2-palette-data"[^>]*>(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert match is not None
    targets = json.loads(match.group(1))
    assert isinstance(targets, list)
    assert any(t.get("url") == "/submit" for t in targets)


@pytest.mark.unit
def test_console_partial_has_all_panels(web_client: object) -> None:
    """``GET /partials/console`` returns 200 with the four surviving panels."""
    response = web_client.get("/partials/console")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    for panel in ("health", "cooking", "kpi", "runs"):
        assert f'data-panel="{panel}"' in body
    # Quick-nav panel removed — it duplicated the left drawer menu.
    assert 'data-panel="nav"' not in body


@pytest.mark.unit
def test_console_partial_is_terminal_log_stream(web_client: object) -> None:
    """Sections render as a terminal log stream, not bordered cards."""
    body = web_client.get("/partials/console").text  # type: ignore[attr-defined]
    assert 'class="console-stream"' in body
    assert "console-line-prompt" in body
    assert 'class="console-out"' in body
    # No card-era markup remains.
    assert "console-panels" not in body
    assert "console-tile" not in body


@pytest.mark.unit
def test_console_partial_empty_state(web_client: object) -> None:
    """Fresh install: the runs panel shows an empty state, not a 500."""
    response = web_client.get("/partials/console")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "No runs yet" in response.text


@pytest.mark.unit
def test_dashboard_preserved_at_new_route(web_client: object) -> None:
    """The classic card-grid dashboard is still reachable at /dashboard."""
    response = web_client.get("/dashboard")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "Recent runs" in response.text


@pytest.mark.unit
def test_console_routes_absent_from_openapi(web_client: object) -> None:
    """No console/dashboard HTML route leaks into the OpenAPI schema."""
    paths = set(web_client.get("/openapi.json").json()["paths"].keys())  # type: ignore[attr-defined]
    assert "/" not in paths
    assert "/dashboard" not in paths
    assert "/partials/console" not in paths
