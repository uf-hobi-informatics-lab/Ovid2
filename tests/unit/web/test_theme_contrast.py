"""WCAG AA contrast guard for every shipped theme palette.

Parses the ``[data-theme="<slug>"]`` token blocks in ``ovid2.css``
(merging a theme's colour block with its personality block, in source
order, exactly as the cascade would) and asserts that the text tokens
stay readable:

- ``--fg``, ``--fg-muted``, ``--fg-subtle``, ``--ok``, ``--warn`` and
  ``--err`` reach >= 4.5:1 on ``--card-bg``;
- ``--accent-fg`` reaches >= 4.5:1 on ``--accent``;
- ``--fg`` reaches >= 4.5:1 on ``--bg``.

A future palette edit therefore cannot silently break readability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static" / "ovid2.css"

_SLUGS = (
    "clinical",
    "matrix",
    "super-mario-world",
    "tatooine",
    "financial",
    "lego",
    "spaceship",
)
_ON_CARD = ("--fg", "--fg-muted", "--fg-subtle", "--ok", "--warn", "--err")
_AA = 4.5

_SELECTOR_PART = re.compile(r'^(?::root|\[data-theme="([a-z0-9-]+)"\])$')
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _theme_tokens() -> dict[str, dict[str, str]]:
    """Return ``slug -> {token: value}`` from the top-level theme blocks."""
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    tokens: dict[str, dict[str, str]] = {slug: {} for slug in _SLUGS}
    for selector, body in _RULE.findall(css):
        slugs: list[str] = []
        for part in (p.strip() for p in selector.split(",")):
            m = _SELECTOR_PART.match(part)
            if m is None:
                slugs = []
                break
            if m.group(1) in tokens:
                slugs.append(m.group(1))
        for slug in slugs:
            for name, value in _DECL.findall(body):
                tokens[slug][name] = value.strip()
    return tokens


def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _hex(tokens: dict[str, str], name: str, slug: str) -> str:
    value = tokens.get(name, "")
    assert _HEX.match(value), f"{slug}: {name} must be a #rrggbb literal, got {value!r}"
    return value


@pytest.mark.unit
@pytest.mark.parametrize("slug", _SLUGS)
def test_theme_palette_meets_wcag_aa(slug: str) -> None:
    """Every text token on its surface is at least WCAG AA (4.5:1)."""
    tokens = _theme_tokens()[slug]
    assert tokens, f"no [data-theme={slug!r}] token block found in ovid2.css"
    card = _hex(tokens, "--card-bg", slug)
    failures: list[str] = []
    for name in _ON_CARD:
        r = _ratio(_hex(tokens, name, slug), card)
        if r < _AA:
            failures.append(f"{name} on --card-bg = {r:.2f}")
    r = _ratio(_hex(tokens, "--accent-fg", slug), _hex(tokens, "--accent", slug))
    if r < _AA:
        failures.append(f"--accent-fg on --accent = {r:.2f}")
    r = _ratio(_hex(tokens, "--fg", slug), _hex(tokens, "--bg", slug))
    if r < _AA:
        failures.append(f"--fg on --bg = {r:.2f}")
    assert not failures, f"{slug} fails WCAG AA: " + "; ".join(failures)


@pytest.mark.unit
def test_contrast_maths_matches_wcag_reference() -> None:
    """Sanity: black on white is 21:1 and #777777 on white is ~4.48:1."""
    assert round(_ratio("#000000", "#ffffff"), 1) == 21.0
    assert 4.4 < _ratio("#777777", "#ffffff") < 4.5
