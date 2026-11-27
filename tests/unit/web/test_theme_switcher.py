"""Unit tests for the client-only theme switcher.

Covers the theming feature layered on top of the v2 shell:

- The topbar renders the switcher control + popover mount (and the
  former day/night-only toggle is gone).
- ``theme.js`` ships the full seven-theme registry (slugs + labels)
  and the custom-colour + guardrail machinery, and stays a classic
  (non-module) script with no third-party imports.
- ``ovid2.css`` declares a ``[data-theme]`` block for each shipped
  named theme.
- The pre-paint script honours any known slug, remaps the retired
  ``tatooine`` / ``industry`` slugs, and re-applies the server-rendered
  custom colour overrides before first paint.
- Appearance is stored per user on the server under ``/api/v1/me/appearance``
  (no other theme route exists).
"""

from __future__ import annotations

import pytest

# The eight shipped themes (movie/game pass + Spaceship + Data Factory).
_SLUGS = [
    "clinical",
    "data-factory",
    "dune",
    "financial",
    "lego",
    "matrix",
    "spaceship",
    "super-mario-world",
]
_LABELS = [
    "Clinical",
    "Data Factory",
    "Dune",
    "Financial",
    "LEGO",
    "Matrix",
    "Spaceship",
    "Super Mario World",
]
# Named themes that are not also the light/dark baseline aliases.
_PALETTE_SLUGS = ["data-factory", "dune", "financial", "lego", "spaceship", "super-mario-world"]
# Retired slugs remapped to their replacements (both in theme.js and
# the base.html pre-paint snippet).
_RETIRED_ALIASES = {"tatooine": "dune", "industry": "lego"}


@pytest.mark.unit
def test_topbar_renders_switcher_control(web_client: object) -> None:
    """The right tools rail hosts the Appearance trigger + editor mount."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'id="tool-appearance"' in body
    assert 'id="appearance-editor"' in body


@pytest.mark.unit
def test_old_day_night_toggle_is_gone(web_client: object) -> None:
    """The former day/night-only button is fully replaced."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'id="theme-toggle"' not in body


@pytest.mark.unit
def test_theme_js_declares_all_themes(web_client: object) -> None:
    """``theme.js`` ships every slug and label in its registry."""
    body = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    for slug in _SLUGS:
        assert f'"{slug}"' in body, f"missing slug {slug!r} in theme.js"
    for label in _LABELS:
        assert label in body, f"missing label {label!r} in theme.js"


@pytest.mark.unit
def test_theme_js_maps_retired_slugs(web_client: object) -> None:
    """``theme.js`` keeps a legacy alias for each retired slug."""
    body = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    for old in _RETIRED_ALIASES:
        assert old in body, f"missing legacy alias for {old!r} in theme.js"


@pytest.mark.unit
def test_theme_js_is_classic_script_without_imports(web_client: object) -> None:
    """``theme.js`` stays a classic script (base.html loads it deferred)."""
    body = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    for line in body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import "), stripped
        assert not stripped.startswith("export "), stripped
    assert "window.Ovid2Theme" in body


@pytest.mark.unit
def test_theme_js_has_custom_override_and_guardrail(web_client: object) -> None:
    """``theme.js`` persists custom colours to the server and derives ink."""
    body = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    assert "/api/v1/me/appearance" in body
    assert "applyCustomOverrides" in body
    assert "readableInk" in body
    assert "contrastRatio" in body


@pytest.mark.unit
def test_css_has_a_block_for_every_theme(web_client: object) -> None:
    """``ovid2.css`` declares a ``[data-theme]`` block per named theme."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    for slug in _PALETTE_SLUGS:
        assert f'[data-theme="{slug}"]' in body, f"missing CSS block for {slug!r}"


@pytest.mark.unit
def test_css_declares_bundled_theme_fonts(web_client: object) -> None:
    """Each per-theme personality face is @font-face'd in ``ovid2.css``."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    for family in (
        "Fredoka",
        "VT323",
        "IBM Plex Sans",
        "IBM Plex Mono",
        "IBM Plex Sans Condensed",
        "Exo 2",
        "Baloo 2",
        "Nunito",
        "JetBrains Mono",
        "Share Tech Mono",
        "Press Start 2P",
        "Michroma",
        "Big Shoulders Stencil",
        "Cinzel",
    ):
        assert f'font-family: "{family}"' in body, f"missing @font-face for {family!r}"


@pytest.mark.unit
def test_prepaint_honours_slug_and_custom(web_client: object) -> None:
    """The pre-paint script applies any known slug + saved overrides."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert "window.OVID2_APPEARANCE" in body
    assert "p.colors" in body
    # Slug list drives the pre-paint attribute set; verify it is no
    # longer hard-limited to light/dark by checking a named theme slug,
    # and that the retired slugs are remapped rather than dropped.
    assert "super-mario-world" in body
    assert '"spaceship"' in body
    for old, new in _RETIRED_ALIASES.items():
        assert old in body, f"pre-paint dropped legacy slug {old!r}"
        assert new in body, f"pre-paint missing replacement {new!r}"


@pytest.mark.unit
def test_appearance_route_is_the_only_theme_surface(web_client: object) -> None:
    """Appearance persists via ``/api/v1/me/appearance`` and nothing else."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = schema.get("paths", {})
    assert set(paths["/api/v1/me/appearance"]) == {"get", "put"}
    for path in paths:
        assert "theme" not in path.lower(), f"unexpected theme route {path!r}"


# Decorative pseudo-elements per theme that must never swallow clicks.
_DECORATIONS = {
    "data-factory": (
        ".topbar::after",
        ".drawer::after",
        ".side-group::before",
        ".nav-item::after",
        ".page-header h1::before",
        ".page-header h1::after",
        ".card::before",
        ".card h2::before",
        ".card h2::after",
        ".kpi::after",
        ".flash::before",
        ".cooking-now .cooking-now-label::before",
        ".live-banner::before",
        ".site-footer::before",
        "body.login-body::before",
        ".login-card::before",
        ".login-card::after",
    ),
    "dune": (
        ".topbar::after",
        ".drawer::after",
        ".side-group::before",
        ".nav-item::after",
        ".page-header h1::after",
        ".card h2::before",
        ".card h2::after",
        ".kpi::before",
        ".flash::before",
        ".cooking-now .cooking-now-label::before",
        ".live-banner::before",
        ".site-footer::before",
        "body.login-body::before",
        "body.login-body::after",
        ".login-card::before",
        ".login-card::after",
    ),
}


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration block of the first rule whose selector is *selector*."""
    start = css.index(selector + " {")
    return css[start : css.index("}", start)]


@pytest.mark.unit
def test_spaceship_scanlines_never_block_clicks(web_client: object) -> None:
    """The full-page CRT scanline overlay is click-through."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    rule = _rule_body(body, 'html[data-theme="spaceship"] body::after')
    assert "position: fixed" in rule
    assert "pointer-events: none" in rule


@pytest.mark.unit
def test_spaceship_glow_follows_the_accent(web_client: object) -> None:
    """The glow helper derives from ``--accent`` so user overrides recolour it."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    assert "--sx-glow: 0 0 12px color-mix(in srgb, var(--accent) 55%, transparent);" in body


@pytest.mark.unit
def test_bundled_font_files_are_served(web_client: object) -> None:
    """Every ``@font-face`` src resolves to a non-empty local file (no CDN)."""
    import re

    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    urls = set(re.findall(r'url\("(/static/fonts/[^"]+\.woff2)"\)', body))
    assert "/static/fonts/jetbrains-mono.woff2" in urls
    for url in sorted(urls):
        r = web_client.get(url)  # type: ignore[attr-defined]
        assert r.status_code == 200 and len(r.content) > 1000, url


@pytest.mark.unit
def test_prepaint_knows_data_factory(web_client: object) -> None:
    """A saved ``data-factory`` slug survives the pre-paint allow-list."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert '"data-factory"' in body


@pytest.mark.unit
def test_theme_signatures_never_block_clicks(web_client: object) -> None:
    """Belts, beacons, ripples, thumper rings and login art are click-through."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    for slug, selectors in _DECORATIONS.items():
        for sel in selectors:
            selector = f'html[data-theme="{slug}"] {sel}'
            assert "pointer-events: none" in _rule_body(body, selector), selector


@pytest.mark.unit
def test_data_factory_effects_follow_the_accent(web_client: object) -> None:
    """Sweep / focus helpers derive from ``--accent`` so user overrides recolour them."""
    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    assert "--df-steel: color-mix(in srgb, var(--accent) 6%, #8c8472);" in body
    assert "--df-accent-wash: color-mix(in srgb, var(--accent) 14%, #fbf9f4);" in body
    assert "--df-cleat: var(--accent);" in body
    assert "--dn-eye: color-mix(in srgb, var(--accent) 62%, #ffffff);" in body
    assert "--dn-tint: color-mix(in srgb, var(--accent) 22%, #efe5d2);" in body


@pytest.mark.unit
def test_theme_animations_have_reduced_motion_off_switch(web_client: object) -> None:
    """Every Data Factory / Dune animated selector is switched off under reduced motion."""
    import re

    body = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    start = body.index("15.z  Reduced motion")
    block = body[start : body.index("Flow graph", start)]
    for slug in ("data-factory", "dune"):
        layer_start = body.index(f" * 15.{slug} ")
        layer = body[layer_start : body.index("/* ---", layer_start + 10)]
        for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", layer):
            if "animation:" not in decls or "@keyframes" in selector:
                continue
            for sel in (s.strip() for s in selector.split(",")):
                if sel.startswith("html["):
                    assert sel in block, f"no reduced-motion off switch for {sel!r}"


@pytest.mark.unit
def test_data_factory_is_the_default_theme(web_client: object) -> None:
    """Without a saved theme, both pre-paint and theme.js fall back to Data Factory."""
    page = web_client.get("/").text  # type: ignore[attr-defined]
    prepaint = page[page.index("window.OVID2_APPEARANCE") : page.index("/static/ovid2.css")]
    assert 'slug = "data-factory";' in prepaint
    assert "prefers-color-scheme" not in prepaint
    js = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    assert 'var DEFAULT_THEME = "data-factory";' in js
    assert "return DEFAULT_THEME;" in js


@pytest.mark.unit
def test_themes_are_listed_alphabetically(web_client: object) -> None:
    """``THEMES`` (and the catalogue enum) are sorted by label, A to Z."""
    import re

    js = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    block = js[js.index("var THEMES = [") : js.index("];", js.index("var THEMES = ["))]
    labels = re.findall(r'label: "([^"]+)"', block)
    assert labels == sorted(labels, key=str.casefold)
    assert "THEMES.sort(" in js
    assert "localeCompare" in js
