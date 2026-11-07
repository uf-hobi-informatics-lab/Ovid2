"""Render the manual to a self-contained static site.

Output layout (``--out``, default ``site/``)::

    index.html
    <chapter>/<page>.html
    assets/…                    # copied from resources/manual/assets
    static/manual.css           # shared with the web panel
    static/manual.js
    static/manual-static.css    # token sheet + standalone chrome
    static/fonts/*.woff2        # IBM Plex Sans, JetBrains Mono, Orbitron
    static/favicon.svg
    search.json
    404.html
    .nojekyll                   # GitHub Pages: serve files verbatim

All URLs are relative, so the tree works from ``file://``, from any
sub-path on a static host (GitHub Pages project sites), or behind
``python -m http.server``. No CDN, no external fetches.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from markupsafe import Markup

from ovid2 import __version__
from ovid2.manual.layout import (
    SEARCH_SURFACE,
    STATIC_SURFACE,
    render_index_fragment,
    render_page_fragment,
    template_env,
)
from ovid2.manual.loader import Manual, load_manual
from ovid2.manual.render import check_links
from ovid2.manual.search import build_search_index

__all__ = ["build_static_site"]

_PKG = Path(__file__).resolve().parent.parent
_WEB_STATIC = _PKG / "web" / "static"
_MANUAL_STATIC = Path(__file__).resolve().parent / "static"
_FONTS = ("ibm-plex-sans.woff2", "jetbrains-mono.woff2", "orbitron.woff2", "OFL.txt")


def _shell(*, title: str, description: str, root: str, fragment: str, built: str) -> str:
    return (
        template_env()
        .get_template("static_shell.html")
        .render(
            title=title,
            description=description,
            root=root,
            fragment=Markup(fragment),  # noqa: S704 — our own rendered fragment
            version=__version__,
            built=built,
        )
    )


def _copy_static(out: Path) -> None:
    static = out / "static"
    (static / "fonts").mkdir(parents=True, exist_ok=True)
    for name in ("manual.css", "manual.js"):
        shutil.copy2(_WEB_STATIC / name, static / name)
    shutil.copy2(_MANUAL_STATIC / "manual-static.css", static / "manual-static.css")
    for font in _FONTS:
        src = _WEB_STATIC / "fonts" / font
        if src.is_file():
            shutil.copy2(src, static / "fonts" / font)
    favicon = _PKG / "resources" / "branding" / "ovid_logo.svg"
    if favicon.is_file():
        shutil.copy2(favicon, static / "favicon.svg")


def build_static_site(out: Path, *, manual: Manual | None = None, strict: bool = True) -> int:
    """Render every manual page into ``out``.

    Args:
        out: Output directory (created; existing contents are replaced).
        manual: Pre-loaded manual (defaults to the packaged one).
        strict: Abort with :class:`ValueError` on any broken link.

    Returns:
        The number of HTML pages written.

    Raises:
        ValueError: ``strict`` and the manual has broken links / anchors.
    """
    manual = manual or load_manual()
    if strict:
        problems = check_links(manual)
        if problems:
            raise ValueError("broken manual links:\n  " + "\n  ".join(problems))

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    built = datetime.now(UTC).strftime("%Y-%m-%d")
    written = 0

    home = _shell(
        title=manual.title,
        description=manual.tagline,
        root="",
        fragment=render_index_fragment(manual, STATIC_SURFACE),
        built=built,
    )
    (out / "index.html").write_text(home, encoding="utf-8")
    written += 1

    for page in manual.ordered_pages():
        surface = STATIC_SURFACE.for_page(page)
        html = _shell(
            title=f"{page.title} · {manual.title}",
            description=page.summary,
            root=surface.root,
            fragment=render_page_fragment(manual, page, surface),
            built=built,
        )
        target = out / f"{page.key}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        written += 1

    not_found = _shell(
        title=f"Not found · {manual.title}",
        description="",
        root="",
        fragment=(
            '<div class="om-shell"><div class="om"><article class="om-article">'
            '<header class="om-header"><h1>Page not found</h1>'
            '<p class="om-lead">That page is not in the manual. '
            'Head back to the <a href="index.html">manual home</a>.</p></header>'
            "</article></div></div>"
        ),
        built=built,
    )
    (out / "404.html").write_text(not_found, encoding="utf-8")

    if manual.asset_dir.is_dir():
        shutil.copytree(manual.asset_dir, out / "assets")
    _copy_static(out)
    (out / "search.json").write_text(
        json.dumps(build_search_index(manual, SEARCH_SURFACE), separators=(",", ":")),
        encoding="utf-8",
    )
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return written
