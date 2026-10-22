"""Contract tests for the web-panel v2 shell.

These invariants defend the fixes that motivated the redesign:

- **Image safety.** Every ``<img>`` tag in a template must carry
  explicit ``width`` and ``height`` HTML attributes. That way a raster
  asset can never render at intrinsic size if the stylesheet is slow
  or fails — the bug that produced the "1254 px logo in the top-left
  corner" report.

- **Token symmetry.** The light and dark theme blocks in
  ``ovid2.css`` must declare identical sets of custom properties.
  A missing token in one theme flat-out breaks that theme's rendering
  in unexpected places.

- **Safety-net CSS.** ``ovid2.css`` must open with (at least)
  ``img { max-width: 100%; height: auto; }`` before any layout
  classes. This is the belt-and-braces backup for the ``<img>``
  attribute rule above: even a raw ``<img>`` inserted by a future
  template author cannot overflow.

- **Static asset packaging.** Every file checked into
  ``src/ovid2/web/static/`` must be reachable at runtime through
  :func:`ovid2.web.templating.static_dir`. A previous stale-wheel
  incident on the production host shipped an install that was missing
  ``theme.js`` entirely; this test would have caught that at commit
  time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The check for width/height applies to every ``<img>`` we ship with
# one small allow-list for future exceptions (e.g. a raw-preview
# component that must inherit its container's dimensions).
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HAS_WIDTH = re.compile(r"\bwidth\s*=", re.IGNORECASE)
_HAS_HEIGHT = re.compile(r"\bheight\s*=", re.IGNORECASE)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

_ALLOWED_IMG_CLASSES: set[str] = set()
# Add class names to the set above when a raw <img> is intentionally
# sized by CSS only. Every entry needs a corresponding CSS rule that
# sets both max-width and height, and a comment explaining why HTML
# attributes could not be used.

_WEB_ROOT: Path = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web"
_TEMPLATES: Path = _WEB_ROOT / "templates"
_STATIC: Path = _WEB_ROOT / "static"
_CSS: Path = _STATIC / "ovid2.css"


@pytest.mark.contract
def test_all_img_tags_have_explicit_dimensions() -> None:
    """Every ``<img>`` in a template carries width + height attributes."""
    offenders: list[str] = []
    for template in sorted(_TEMPLATES.glob("*.html")):
        text = template.read_text(encoding="utf-8")
        # Strip Jinja + HTML comments so we do not lint documentation examples.
        text = _JINJA_COMMENT.sub("", text)
        text = _HTML_COMMENT.sub("", text)
        for tag in _IMG_TAG.findall(text):
            if _HAS_WIDTH.search(tag) and _HAS_HEIGHT.search(tag):
                continue
            # Allow tags with an explicit allow-list class.
            cls_match = re.search(r'class\s*=\s*"([^"]+)"', tag)
            classes = set((cls_match.group(1) if cls_match else "").split())
            if classes & _ALLOWED_IMG_CLASSES:
                continue
            offenders.append(f"{template.name}: {tag}")

    assert not offenders, (
        "Every <img> must declare width and height HTML attributes so "
        "the browser has intrinsic dimensions even before CSS parses. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.contract
def test_light_and_dark_theme_tokens_are_symmetric() -> None:
    """Both theme blocks in ``ovid2.css`` declare the same custom properties."""
    text = _CSS.read_text(encoding="utf-8")

    def extract_block(selector: str) -> set[str]:
        # Match ``selector { ... }`` non-greedily.
        pattern = re.compile(
            re.escape(selector) + r"\s*\{(.*?)\}", re.DOTALL
        )
        match = pattern.search(text)
        assert match, f"could not find block for {selector!r}"
        body = match.group(1)
        return set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", body))

    light = extract_block('[data-theme="light"]')
    dark = extract_block('[data-theme="dark"]')

    only_in_light = light - dark
    only_in_dark = dark - light
    assert not only_in_light and not only_in_dark, (
        f"Theme token sets diverge.\n"
        f"  only in light: {sorted(only_in_light)}\n"
        f"  only in dark : {sorted(only_in_dark)}"
    )


@pytest.mark.contract
def test_css_has_image_safety_net_rule() -> None:
    """``ovid2.css`` opens with a rule capping every ``<img>``.

    The rule must appear before the ``.topbar`` / ``.drawer`` layout
    classes — otherwise a race where the browser applies partial CSS
    could still let an image render at its intrinsic size.
    """
    text = _CSS.read_text(encoding="utf-8")
    safety_pattern = re.compile(
        r"img[^{]*\{[^}]*max-width\s*:\s*100%[^}]*\}", re.DOTALL
    )
    safety_match = safety_pattern.search(text)
    assert safety_match, (
        "ovid2.css must declare `img { max-width: 100%; height: auto; }` "
        "as a global safety net."
    )
    topbar_pos = text.find(".topbar")
    assert topbar_pos != -1
    assert safety_match.start() < topbar_pos, (
        "The image safety-net rule must come before any layout classes "
        "so that partial-CSS races cannot let a raster asset overflow."
    )


@pytest.mark.contract
def test_all_static_assets_are_packaged() -> None:
    """Every file in ``src/ovid2/web/static/`` is reachable at runtime.

    Regression guard for the stale-wheel incident where ``theme.js``
    was missing from the deployed install even though it existed in
    the source tree.
    """
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from ovid2.web.templating import static_dir

    runtime_dir = static_dir()
    assert runtime_dir.is_dir(), (
        f"static_dir() reports {runtime_dir!s} which is not a directory"
    )

    missing: list[str] = []
    for source_file in sorted(_STATIC.iterdir()):
        if not source_file.is_file():
            continue
        runtime_file = runtime_dir / source_file.name
        if not runtime_file.is_file() or runtime_file.stat().st_size == 0:
            missing.append(source_file.name)

    assert not missing, (
        "The following static assets exist in the source tree but are "
        "missing (or empty) at runtime — likely a packaging drift:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.contract
def test_base_template_declares_responsive_meta() -> None:
    """``base.html`` opts into responsive rendering on mobile.

    Without the ``viewport`` meta tag mobile browsers zoom the layout
    to desktop width, defeating the drawer collapse.
    """
    base = _TEMPLATES / "base.html"
    text = base.read_text(encoding="utf-8")
    assert 'name="viewport"' in text
    assert "width=device-width" in text
    # The topbar-first shell.
    assert 'class="topbar"' in text
    assert 'class="drawer"' in text
    assert 'id="main"' in text
