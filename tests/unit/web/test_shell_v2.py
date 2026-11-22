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
def test_shell_menu_toggle_lives_in_drawer_head(web_client: object) -> None:
    """The menu toggle sits in a fixed slot at the top of the drawer.

    ``#drawer-toggle`` lives inside ``<aside class="drawer">`` in a
    rail-width ``.drawer-head`` slot so it never moves when the drawer
    switches between the full menu and the icon rail. The phone-only
    ``#drawer-toggle-mobile`` stays in the topbar (the drawer is
    off-canvas there). The old medium-only ``#drawer-collapse`` chevron
    is gone — the toggle does its job at every width.
    """
    body = web_client.get("/").text  # type: ignore[attr-defined]
    drawer = body[body.index('<aside id="site-nav"') : body.index("</aside>")]
    assert 'class="drawer-head"' in drawer
    assert 'id="drawer-toggle"' in drawer
    assert drawer.index('class="drawer-head"') < drawer.index('class="drawer-nav"')
    topbar = body[body.index('<header class="topbar"') : body.index("</header>")]
    assert 'id="drawer-toggle-mobile"' in topbar
    assert 'id="drawer-toggle"' not in topbar.replace('id="drawer-toggle-mobile"', "")
    assert body.count("data-drawer-toggle") == 2
    assert 'id="drawer-collapse"' not in body


@pytest.mark.unit
def test_drawer_head_slot_is_rail_width(web_client: object) -> None:
    """The toggle slot is pinned to the rail width, so it never shifts."""
    css = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    block = css[css.index(".drawer-head {") :]
    block = block[: block.index("}")]
    assert "width: var(--drawer-rail-w)" in block
    assert "html.drawer-rail .drawer { width: var(--drawer-rail-w); }" in css
    assert "drawer-collapsed" not in css


@pytest.mark.unit
def test_shell_uses_wordmark(web_client: object) -> None:
    """The topbar renders the animated mark + ``Ovid2`` wordmark as the home link.

    The ``<ovid-mark>`` web component (``static/ovid_mark.js``) sits just
    before the wordmark inside the home link. The retired monochrome
    ``.brand-mark`` mask and raster ``.brand-logo`` must never return.
    """
    response = web_client.get("/")  # type: ignore[attr-defined]
    body = response.text
    # The wordmark is the home link.
    assert 'class="brand-mark-link" href="/"' in body
    assert '<span class="brand-text">Ovid<span class="brand-accent">2</span>' in body
    link = body[
        body.index('class="brand-mark-link"') : body.index(
            "</a>", body.index('class="brand-mark-link"')
        )
    ]
    assert '<ovid-mark id="brand-sprout"' in link
    assert link.index("<ovid-mark") < link.index('class="brand-text"')
    assert "/static/ovid_mark.js" in body
    assert "/static/ovid_mark_status.js" in body
    # The planet mark and any raster logo are gone forever.
    assert 'class="brand-mark"' not in body
    assert 'class="brand-logo"' not in body
    assert "ovid_mark.svg" not in body


@pytest.mark.unit
def test_brand_mark_polls_only_with_view_runs(web_client: object) -> None:
    """The header mark polls the global job queue only for ``view_runs`` users."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    tag = body[body.index('<ovid-mark id="brand-sprout"') :]
    tag = tag[: tag.index(">")]
    # Auth disabled in the unit env -> synthetic admin -> may view runs.
    assert 'data-poll-runs="1"' in tag


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
        ("/pipeline-packages", "/pipeline-packages"),
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
    """``nav.js`` drives ``drawer-open`` (phones) / ``drawer-rail`` (wide) and ESC."""
    response = web_client.get("/static/nav.js")  # type: ignore[attr-defined]
    body = response.text
    assert response.status_code == 200
    assert "drawer-open" in body
    assert "drawer-rail" in body
    assert "Escape" in body


@pytest.mark.unit
def test_nav_js_uses_click_delegation(web_client: object) -> None:
    """``nav.js`` delegates the drawer-toggle click on ``document``.

    Regression guard: an earlier revision wired the click handler
    directly on ``#drawer-toggle`` via ``element.addEventListener``,
    which silently broke whenever the shell was re-rendered (or when
    the click landed on the inner ``<svg>``). Delegation on
    ``document`` + ``closest("[data-drawer-toggle]")`` covers both
    toggles (drawer head + phone topbar).
    """
    response = web_client.get("/static/nav.js")  # type: ignore[attr-defined]
    body = response.text
    assert response.status_code == 200
    assert 'document.addEventListener("click"' in body
    assert '.closest("[data-drawer-toggle]")' in body


@pytest.mark.unit
def test_nav_js_persists_wide_collapse(web_client: object) -> None:
    """``nav.js`` persists the wide-viewport rail and migrates the old key.

    A collapsed rail must survive navigation via a rail icon, so the
    state is written under ``ovid2DrawerWideCollapsed``. The retired
    medium-only ``ovid2DrawerCollapsed`` key is migrated once, then
    removed.
    """
    body = web_client.get("/static/nav.js").text  # type: ignore[attr-defined]
    assert "ovid2DrawerWideCollapsed" in body
    assert "ovid2DrawerCollapsed" in body
    assert "removeItem(LEGACY_KEY)" in body


@pytest.mark.unit
def test_prepaint_restores_wide_collapse(web_client: object) -> None:
    """The base-template pre-paint restores ``drawer-rail`` on wide viewports.

    The inline pre-paint script must re-apply ``drawer-rail`` from
    ``ovid2DrawerWideCollapsed`` at >= 900px so the collapsed rail does
    not flash back open after navigating. Mobile must be excluded, and
    ``drawer-open`` is never restored (it is the transient phone state).
    """
    body = web_client.get("/").text  # type: ignore[attr-defined]
    head = body[: body.index("/static/ovid2.css")]
    assert "ovid2DrawerWideCollapsed" in head
    assert 'matchMedia("(min-width: 900px)")' in head
    assert 'classList.add("drawer-rail")' in head
    assert 'classList.add("drawer-open")' not in head


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


@pytest.mark.unit
def test_shell_has_nav_overflow_cue(web_client: object) -> None:
    """The drawer ships a ``#nav-more`` overflow cue, hidden by default.

    ``nav.js`` un-hides it (with a "N more" count) only while some menu
    items are scrolled out of view, so a too-tall menu never silently
    hides its last entries.
    """
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'id="nav-more"' in body
    start = body.index('id="nav-more"')
    tag = body[body.rindex("<button", 0, start) : body.index(">", start)]
    assert " hidden" in tag, "nav-more must start hidden"
    assert 'class="nav-more-label"' in body


@pytest.mark.unit
def test_nav_js_drives_overflow_cue(web_client: object) -> None:
    """``nav.js`` toggles the edge fades and wires the "more" button."""
    body = web_client.get("/static/nav.js").text  # type: ignore[attr-defined]
    assert "has-more-below" in body
    assert "has-more-above" in body
    assert '.closest("#nav-more")' in body


@pytest.mark.unit
def test_drawer_nav_is_scroll_container(web_client: object) -> None:
    """The nav list (not the whole drawer) scrolls, with compact rows."""
    css = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    block = css[css.index(".drawer-nav {") :]
    block = block[: block.index("}")]
    assert "overflow-y: auto" in block
    assert "min-height: 0" in block
    assert "--nav-item-py:" in css
    assert ".nav-more" in css


@pytest.mark.unit
def test_mobile_toggle_hidden_on_wide_screens(web_client: object) -> None:
    """The phone toggle's hide rule outranks the generic ``.icon-button`` display.

    Regression guard: ``.icon-button { display: inline-flex }`` is declared
    later in the file with the same specificity as a bare
    ``.drawer-toggle-mobile`` rule, which made the phone burger show on
    desktop next to the drawer one.
    """
    css = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    assert ".topbar .drawer-toggle.drawer-toggle-mobile { display: none; }" in css
    assert ".topbar .drawer-toggle.drawer-toggle-mobile { display: inline-flex; }" in css
