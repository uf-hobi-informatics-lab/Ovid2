"""Contract tests for the web panel's full-screen mode.

Full screen is *real* browser full screen with the whole panel kept. To
survive link clicks (browsers drop full screen on every page load) the
app runs inside a same-origin full-window frame while full screen. These
tests pin:

- ``base.html`` carries the top-bar toggle and loads ``fullscreen.js``,
  and no longer hides any shell region (no ``focus-mode``);
- ``fullscreen.js`` exposes ``window.Ovid2Fullscreen``, binds the
  Ctrl/Cmd+Shift+F shortcut, uses the Fullscreen API, frames the app,
  only trusts same-origin messages, and keeps sign-in / logout out of
  the frame;
- the app sends ``frame-ancestors 'self'`` so only the panel can frame
  itself.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web"
_BASE = (_WEB / "templates" / "base.html").read_text(encoding="utf-8")
_JS = (_WEB / "static" / "fullscreen.js").read_text(encoding="utf-8")
_CSS = (_WEB / "static" / "ovid2.css").read_text(encoding="utf-8")
_ICONS = (_WEB / "templates" / "_icons.html").read_text(encoding="utf-8")
_APP = (_WEB / "app.py").read_text(encoding="utf-8")


def test_base_has_toggle_and_script() -> None:
    """Every page gets the top-bar toggle and the module."""
    topbar = _BASE[_BASE.index('<header class="topbar"') : _BASE.index("</header>")]
    assert "data-fullscreen-toggle" in topbar, "toggle must sit in the top bar"
    assert '<script src="/static/fullscreen.js" defer></script>' in _BASE


def test_toggle_is_not_gated_on_auth() -> None:
    """The toggle renders whether or not sign-in is enabled."""
    toggle_at = _BASE.index("data-fullscreen-toggle")
    auth_block = _BASE.index("{% if auth_enabled() and _user_ctx")
    auth_end = _BASE.index("{% endif %}", auth_block)
    assert not auth_block < toggle_at < auth_end


def test_full_screen_keeps_the_panel_chrome() -> None:
    """Full screen hides only the browser — never the drawer / rail / bars."""
    assert "focus-mode" not in _BASE
    assert "focus-mode" not in _CSS
    assert "focus-mode" not in _JS
    for sel in (".topbar", ".drawer", ".tools-rail", ".site-footer"):
        assert not re.search(rf"html\.fs-(host|framed)\s+{re.escape(sel)}\b", _CSS), sel


def test_js_api_and_bindings() -> None:
    """Shortcut, Fullscreen API, frame host and the exported API."""
    assert "window.Ovid2Fullscreen" in _JS
    for name in ("enter", "exit", "toggle", "active"):
        assert re.search(rf"\b{name}:\s*{name}\b", _JS), name
    assert "shiftKey" in _JS and "KeyF" in _JS
    assert "fullscreenchange" in _JS
    assert "requestFullscreen" in _JS and "exitFullscreen" in _JS
    assert 'FRAME_ID = "fs-frame"' in _JS
    assert ".style.display" not in _JS, "toggle a class / hidden, never inline display (U1)"


def test_js_message_and_link_safety() -> None:
    """Only same-origin frame messages are honoured; auth pages leave the frame."""
    assert "ev.source !== frame.contentWindow" in _JS
    assert "ev.origin !== window.location.origin" in _JS
    assert "postMessage(data, window.location.origin)" in _JS
    assert re.search(r"TOP_ONLY = /\^\\/\(logout\|login\|auth\)", _JS)
    assert '"noopener"' in _JS


def test_frame_ancestors_header_installed() -> None:
    """The app adds the same-origin framing headers."""
    assert "FrameAncestorsMiddleware" in _APP


def test_icons_exist() -> None:
    """The toggle uses real icons, not the fallback."""
    assert 'name == "maximize"' in _ICONS
    assert 'name == "minimize"' in _ICONS
