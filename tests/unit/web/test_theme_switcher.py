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
  ``dune`` / ``industry`` slugs, and re-applies the server-rendered
  custom colour overrides before first paint.
- Appearance is stored per user on the server under ``/api/v1/me/appearance``
  (no other theme route exists).
"""

from __future__ import annotations

import pytest

# The seven shipped themes (movie/game pass + Spaceship).
_SLUGS = [
    "clinical",
    "matrix",
    "super-mario-world",
    "tatooine",
    "financial",
    "lego",
    "spaceship",
]
_LABELS = [
    "Clinical",
    "Matrix",
    "Super Mario World",
    "Star Wars / Tatooine",
    "Financial",
    "LEGO",
    "Spaceship",
]
# Named themes that are not also the light/dark baseline aliases.
_PALETTE_SLUGS = ["super-mario-world", "tatooine", "financial", "lego", "spaceship"]
# Retired slugs remapped to their replacements (both in theme.js and
# the base.html pre-paint snippet).
_RETIRED_ALIASES = {"dune": "tatooine", "industry": "lego"}


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
