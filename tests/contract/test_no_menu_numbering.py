"""Contract: no theme numbers the left menu.

Menu items and group headings carry names, not numbers. No theme may
number the drawer (Financial's old F1…Fn keys, Data Factory's "ST-01"
stations, Dune's Roman numerals) with CSS counters or generated digits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "static" / "ovid2.css"
_NAV = re.compile(r"\.(?:drawer|drawer-nav|side-group|nav-item|nav-label)\b")
_THEME = re.compile(r'data-theme="([a-z0-9-]+)"')
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_NUMBERING = re.compile(
    r"counter-(?:reset|increment|set)\s*:\s*(?!none)|counter\(|counters\("
    r'|content\s*:\s*"[^"]*\d'
)
# CSS escapes (``"\25C2"`` = ▸) are glyphs, not numbering.
_ESCAPE = re.compile(r"\\[0-9a-fA-F]{1,6}\s?")


def _rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Flatten one level of @media / @supports nesting.
    css = re.sub(r"@(?:media|supports)[^{]*\{", "", css)
    return [(sel.strip(), body) for sel, body in _RULE.findall(css)]


@pytest.mark.contract
def test_no_theme_numbers_the_menu() -> None:
    """No theme rule on the drawer / nav numbers its items or headings."""
    offenders = []
    for selector, body in _rules(_CSS.read_text(encoding="utf-8")):
        if not _NUMBERING.search(_ESCAPE.sub("", body)):
            continue
        for part in selector.split(","):
            themes = set(_THEME.findall(part))
            if themes and _NAV.search(part):
                offenders.append(part.strip())
    assert not offenders, f"menu numbering in a theme: {offenders}"
