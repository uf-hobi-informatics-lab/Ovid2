"""Unit tests for the client-only theme switcher.

Covers the theming feature layered on top of the v2 shell:

- The topbar renders the switcher control + popover mount (and the
  former day/night-only toggle is gone).
- ``theme.js`` ships the full six-theme registry (slugs + labels)
  and the custom-colour + guardrail machinery, and stays a classic
  (non-module) script with no third-party imports.
- ``ovid2.css`` declares a ``[data-theme]`` block for each shipped
  named theme.
- The pre-paint script honours any known slug, remaps the retired
  ``dune`` / ``industry`` slugs, and re-applies saved custom colour
  overrides before first paint.
- The change is client-only: no theme route leaked into the schema.
"""

from __future__ import annotations

import pytest

# The six shipped themes after the movie/game theme pass.
_SLUGS = [
    "clinical",
    "matrix",
    "super-mario-world",
    "tatooine",
    "financial",
    "lego",
]
_LABELS = [
    "Clinical",
    "Matrix",
    "Super Mario World",
    "Star Wars / Tatooine",
    "Financial",
    "LEGO",
]
# Named themes that are not also the light/dark baseline aliases.
_PALETTE_SLUGS = ["super-mario-world", "tatooine", "financial", "lego"]
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
    """``theme.js`` persists custom colours and derives readable ink."""
    body = web_client.get("/static/theme.js").text  # type: ignore[attr-defined]
    assert "ovid2ThemeCustom" in body
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
    for family in ("Fredoka", "VT323", "Inter", "Exo 2", "Baloo 2", "Nunito"):
        assert f'font-family: "{family}"' in body, f"missing @font-face for {family!r}"


@pytest.mark.unit
def test_prepaint_honours_slug_and_custom(web_client: object) -> None:
    """The pre-paint script applies any known slug + saved overrides."""
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert "ovid2ThemeCustom" in body
    # Slug list drives the pre-paint attribute set; verify it is no
    # longer hard-limited to light/dark by checking a named theme slug,
    # and that the retired slugs are remapped rather than dropped.
    assert "super-mario-world" in body
    for old, new in _RETIRED_ALIASES.items():
        assert old in body, f"pre-paint dropped legacy slug {old!r}"
        assert new in body, f"pre-paint missing replacement {new!r}"


@pytest.mark.unit
def test_no_theme_route_in_schema(web_client: object) -> None:
    """The feature is client-only: no theme endpoint entered the schema."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = schema.get("paths", {})
    for path in paths:
        assert "theme" not in path.lower(), f"unexpected theme route {path!r}"
