"""Contract: every shipped page theme declares the full baseline token set.

``test_web_shell_v2.py`` only compares the clinical (light) and matrix
(dark) blocks. This contract extends the guarantee to every slug in
``static/theme.js`` ``THEMES``: the *first* ``[data-theme="<slug>"]``
colour block of each theme must declare at least every custom property
the clinical baseline block declares, so no theme silently inherits a
clinical value (a light ``--card-bg`` inside a dark theme, say).

Theme-only extras (``--ovid-*`` mark colours, ``--sx-*`` helpers…) are
allowed; missing baseline tokens are not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web"
_CSS = _WEB / "static" / "ovid2.css"
_THEME_JS = _WEB / "static" / "theme.js"


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _first_block_tokens(css: str, slug: str) -> set[str]:
    pattern = re.compile(
        r'(?:^|\n)\s*(?:[^{}]*,\s*)?\[data-theme="' + re.escape(slug) + r'"\][^{}]*\{([^{}]*)\}'
    )
    match = pattern.search(css)
    assert match, f"no [data-theme={slug!r}] block in ovid2.css"
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", match.group(1)))


def _theme_slugs() -> list[str]:
    js = _THEME_JS.read_text(encoding="utf-8")
    block = js[js.index("var THEMES = [") : js.index("];", js.index("var THEMES = ["))]
    return re.findall(r'slug:\s*"([a-z0-9-]+)"', block)


@pytest.mark.contract
def test_theme_js_lists_at_least_the_baseline_themes() -> None:
    """Sanity: the slug parser sees the registry (guards a silent no-op)."""
    slugs = _theme_slugs()
    assert "clinical" in slugs and "matrix" in slugs
    assert len(slugs) >= 8


@pytest.mark.contract
@pytest.mark.parametrize("slug", _theme_slugs())
def test_every_theme_declares_the_baseline_tokens(slug: str) -> None:
    """Each theme's colour block is a superset of the clinical baseline."""
    css = _strip_comments(_CSS.read_text(encoding="utf-8"))
    baseline = _first_block_tokens(css, "clinical")
    tokens = _first_block_tokens(css, slug)
    missing = sorted(baseline - tokens)
    assert not missing, f"theme {slug!r} is missing baseline tokens: {missing}"
