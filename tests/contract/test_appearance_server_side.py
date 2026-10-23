"""Contract: appearance is stored per user on the server, never in the browser.

The Appearance panel's theme, quick colours, per-component overrides and
saved themes follow the user to any browser or laptop, so their single
source of truth is ``web_user_prefs`` (``/api/v1/me/appearance``), rendered
into every full page by ``_appearance_prepaint.html``.

Pinned here:

1. No web template or static script *writes* an appearance key to
   ``localStorage`` / ``sessionStorage``. The only browser-storage access
   to the legacy keys allowed is in ``theme.js``'s one-time migration,
   which reads and then removes them.
2. Every standalone full page (``base.html``, ``login.html``,
   ``pending.html``) includes the server-rendered pre-paint partial
   *before* the stylesheet link.
3. The browser store saves to the server: ``theme.js`` PUTs
   ``/api/v1/me/appearance`` and the route exists with GET + PUT.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web"
_STATIC = _WEB / "static"
_TEMPLATES = _WEB / "templates"
_SNAPSHOT = Path(__file__).parent / "data" / "openapi.snapshot.json"

_APPEARANCE_KEYS = ("ovid2Theme", "ovid2ThemeCustom", "ovid2Appearance", "ovid2SavedThemes")
_WRITE_RE = re.compile(r"(localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*[\"']([A-Za-z]+)[\"']")


def _sources() -> list[Path]:
    files = sorted(_STATIC.glob("*.js")) + sorted(_TEMPLATES.rglob("*.html"))
    return [p for p in files if not p.name.endswith(".min.js")]


@pytest.mark.contract
def test_no_appearance_writes_to_browser_storage() -> None:
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for m in _WRITE_RE.finditer(text):
            if m.group(2) in _APPEARANCE_KEYS:
                offenders.append(f"{path.name}: {m.group(0)}")
        for key in _APPEARANCE_KEYS:
            if f'"{key}"' in text and path.name != "theme.js":
                offenders.append(f"{path.name}: mentions {key}")
    assert not offenders, "appearance must persist server-side:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_legacy_keys_are_only_migrated_and_removed() -> None:
    js = (_STATIC / "theme.js").read_text(encoding="utf-8")
    assert "LEGACY_KEYS" in js
    assert "localStorage.removeItem(k)" in js
    assert "localStorage.setItem" not in js


@pytest.mark.contract
@pytest.mark.parametrize("page", ["base.html", "login.html", "pending.html"])
def test_full_pages_include_server_prepaint_before_css(page: str) -> None:
    html = (_TEMPLATES / page).read_text(encoding="utf-8")
    inc = html.find('{% include "_appearance_prepaint.html" %}')
    css = html.find("/static/ovid2.css")
    assert inc != -1, f"{page} must include _appearance_prepaint.html"
    assert inc < css, f"{page}: pre-paint must precede the stylesheet"


@pytest.mark.contract
def test_store_saves_to_server_route() -> None:
    js = (_STATIC / "theme.js").read_text(encoding="utf-8")
    assert '"/api/v1/me/appearance"' in js and 'method: "PUT"' in js
    spec = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert {"get", "put"} <= set(spec["paths"]["/api/v1/me/appearance"])
