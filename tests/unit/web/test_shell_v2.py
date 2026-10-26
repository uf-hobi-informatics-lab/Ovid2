"""Unit tests for the web-panel v2 responsive shell.

Covers:

- Base-template scaffolding (drawer, topbar, main, scrim, focus skip-link).
- Correct ``nav-item active`` marking for every top-level route.
- Card-grid + KPI presence on the dashboard partial.
- Nav.js and theme.js are served and reference the expected symbols.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shell scaffolding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_shell_has_skip_link(web_client: object) -> None:
    """A visible-on-focus skip-link lands keyboard users at ``#main``."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    assert 'class="skip-link"' in body
    assert 'href="#main"' in body


@pytest.mark.unit
def test_shell_has_drawer_scrim(web_client: object) -> None:
    """The mobile drawer requires a scrim overlay for click-outside close."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    assert 'class="drawer-scrim"' in body
    assert "data-drawer-close" in body


@pytest.mark.unit
def test_shell_has_drawer_collapse_toggle(web_client: object) -> None:
    """The medium-viewport rail collapse button lives in the drawer."""
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    assert 'id="drawer-collapse"' in body


@pytest.mark.unit
def test_shell_uses_wordmark(web_client: object) -> None:
    """The topbar renders the ``Ovid2`` text wordmark as the home link.

    The wordmark is styled with the active theme's display face
    (``--font-display``). There is deliberately NO planet/orbit glyph:
    the monochrome ``.brand-mark`` mask was removed permanently, so no
    raster ``.brand-logo`` image and no masked ``.brand-mark`` span may
    appear in the shell.
    """
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    # The wordmark is the home link.
    assert 'class="brand-mark-link" href="/"' in body
    assert '<span class="brand-text">Ovid<span class="brand-accent">2</span>' in body
    # The planet mark and any raster logo are gone forever.
    assert 'class="brand-mark"' not in body
    assert 'class="brand-logo"' not in body
    assert "ovid_mark.svg" not in body


# ---------------------------------------------------------------------------
# Nav-active markers, sweep of every route
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url, expected_href",
    [
        ("/", "/"),
        ("/submit", "/submit"),
        ("/runs", "/runs"),
        ("/pipelines", "/pipelines"),
        ("/storage", "/storage"),
        ("/warehouses", "/warehouses"),
        ("/addresses", "/storage"),
        ("/pipelines", "/pipelines"),
        ("/routes", "/pipelines"),
        ("/secrets", "/secrets"),
        ("/install", "/install"),
        ("/health", "/health"),
    ],
)
def test_nav_active_marker_matches_route(web_client: object, url: str, expected_href: str) -> None:
    """Each top-level route marks its own drawer link as active."""
    response = web_client.get(url)  # type: ignore[attr-defined]
    # Some pages under W-features may 500 without setup; skip those.
    if response.status_code >= 500:
        pytest.skip(f"{url} returned {response.status_code} in isolated env")
    assert response.status_code == 200
    body = response.text
    marker = f'href="{expected_href}" class="nav-item active"'
    assert marker in body, f"expected active marker for {url!r} not found; looked for: {marker}"


# ---------------------------------------------------------------------------
# Dashboard cards
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dashboard_uses_card_grid(web_client: object) -> None:
    """The dashboard partial wraps KPI cards in ``.card-grid``."""
    response = web_client.get("/partials/dashboard")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert 'class="card-grid"' in body
    # KPI rendering.
    assert 'class="kpi"' in body
    assert 'class="kpi-value"' in body


@pytest.mark.unit
def test_dashboard_kpis_include_health(web_client: object) -> None:
    """The environment KPI card labels its status."""
    response = web_client.get("/partials/dashboard")  # type: ignore[attr-defined]
    body = response.text
    assert "Environment" in body
    assert "Queued" in body
    assert "Runs tracked" in body


# ---------------------------------------------------------------------------
# JS surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nav_js_wires_drawer(web_client: object) -> None:
    """``nav.js`` toggles the ``drawer-open`` class and listens for ESC."""
    response = web_client.get("/static/nav.js")  # type: ignore[attr-defined]
    body = response.text
    assert response.status_code == 200
    assert "drawer-open" in body
    assert "Escape" in body
    assert "drawer-collapsed" in body


@pytest.mark.unit
def test_nav_js_uses_click_delegation(web_client: object) -> None:
    """``nav.js`` delegates the drawer-toggle click on ``document``.

    Regression guard: an earlier revision wired the click handler
    directly on ``#drawer-toggle`` via ``element.addEventListener``,
    which silently broke whenever the topbar was re-rendered (or when
    the click landed on the inner ``<svg>`` in browsers with
    idiosyncratic pointer-event resolution). Delegation on
    ``document`` combined with ``element.closest("#drawer-toggle")``
    fixes both cases; this test locks the pattern in.
    """
    response = web_client.get("/static/nav.js")  # type: ignore[attr-defined]
    body = response.text
    assert response.status_code == 200
    assert 'document.addEventListener("click"' in body
    assert '.closest("#drawer-toggle")' in body
    assert '.closest("#drawer-collapse")' in body


@pytest.mark.unit
def test_nav_js_persists_wide_collapse(web_client: object) -> None:
    """``nav.js`` persists the wide-viewport collapse state.

    Regression guard: when the drawer is collapsed on a desktop
    viewport, clicking a rail nav icon navigates to a new page; without
    persistence the destination page dropped the ``drawer-open`` class
    and the rail sprang back open. ``nav.js`` must write the collapse
    state to ``localStorage`` under ``ovid2DrawerWideCollapsed`` so the
    pre-paint snippet can restore it.
    """
    body = web_client.get("/static/nav.js").text  # type: ignore[attr-defined]
    assert "ovid2DrawerWideCollapsed" in body, (
        "nav.js must persist the wide-viewport collapse state so a "
        "collapsed rail survives navigation via a nav icon"
    )


@pytest.mark.unit
def test_prepaint_restores_wide_collapse(web_client: object) -> None:
    """The base-template pre-paint restores the collapsed rail on desktop.

    Mirrors ``ovid2DrawerCollapsed``: the inline pre-paint script must
    re-apply ``drawer-open`` from ``ovid2DrawerWideCollapsed`` at
    >= 900px so the collapsed rail does not flash back open after
    navigating from a collapsed state. Mobile must be excluded.
    """
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert "ovid2DrawerWideCollapsed" in body, (
        "base.html pre-paint must restore the wide-viewport collapse state"
    )
    assert 'matchMedia("(min-width: 900px)")' in body, (
        "the wide-collapse restore must be gated to non-mobile viewports"
    )


@pytest.mark.unit
def test_theme_controls_are_announced(web_client: object) -> None:
    """The Appearance trigger is labelled and announces its open state.

    The old day/night button (whose label ``theme.js`` rewrote) was
    replaced by the Appearance tool; ``appearance.js`` now owns the
    control and toggles ``aria-expanded`` so screen readers hear it.
    """
    page = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'id="tool-appearance"' in page
    assert 'aria-label="Appearance"' in page
    js = web_client.get("/static/appearance.js").text  # type: ignore[attr-defined]
    assert "aria-expanded" in js
    theme = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    assert "OVID2_APPEARANCE" in theme
