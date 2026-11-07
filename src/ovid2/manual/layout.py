"""Shared page layout for both manual delivery surfaces.

The web panel and the static site render *the same* article fragment
(sidebar · article · on-this-page rail) from
``ovid2/manual/templates/``; only the outer shell differs:

- web: ``web/templates/manual.html`` extends the panel's ``base.html`` and
  drops the fragment into ``<main>``;
- static: ``templates/static_shell.html`` is a standalone HTML document.

:class:`Surface` captures everything that differs between the two (URL
scheme, asset prefix, whether the edit link and the theme toggle show).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from ovid2.manual.loader import Manual, Page
from ovid2.manual.render import RenderedPage, render_markdown, render_page

__all__ = ["Surface", "render_index_fragment", "render_page_fragment", "template_env"]

_TEMPLATES = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class Surface:
    """URL scheme of one delivery surface.

    Attributes:
        name: ``"web"`` or ``"static"``.
        root: URL of the manual home, relative to the *current page* for the
            static site (``""`` / ``"../"``) and absolute for the web
            (``"/manual/"``).
        suffix: Page URL suffix (``""`` for web, ``".html"`` for static).
    """

    name: str
    root: str
    suffix: str

    def home_url(self) -> str:
        """Return the manual landing-page URL."""
        return self.root + ("index.html" if self.name == "static" else "")

    def page_url(self, key: str, anchor: str = "") -> str:
        """Return the URL of page ``key``."""
        return f"{self.root}{key}{self.suffix}" + (f"#{anchor}" if anchor else "")

    def asset_url(self, relpath: str) -> str:
        """Return the URL of a manual asset."""
        return f"{self.root}assets/{relpath}"

    def search_url(self) -> str:
        """Return the URL of the search index."""
        return f"{self.root}search.json"

    def for_page(self, page: Page | None) -> Surface:
        """Return the surface re-rooted for ``page`` (static depth handling)."""
        if self.name != "static" or page is None:
            return self
        depth = page.key.count("/")
        return Surface(self.name, "../" * depth, self.suffix)


WEB_SURFACE = Surface("web", "/manual/", "")
STATIC_SURFACE = Surface("static", "", ".html")
#: Root-relative resolver used for the search index (client joins with root).
SEARCH_SURFACE = Surface("search", "", "")


@lru_cache(maxsize=1)
def template_env() -> Environment:
    """Return the Jinja environment for the manual templates."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md_inline"] = lambda text: Markup(render_markdown(text or ""))  # noqa: S704 — trusted repo Markdown
    return env


def _common(manual: Manual, surface: Surface) -> dict[str, Any]:
    return {"manual": manual, "surface": surface}


def _rendered(manual: Manual, page: Page, surface: Surface) -> RenderedPage:
    cache_key = f"{surface.name}:{surface.root}:{page.key}"
    return manual.cached(cache_key, lambda: render_page(page, manual, surface))  # type: ignore[no-any-return]


def render_page_fragment(manual: Manual, page: Page, surface: Surface) -> str:
    """Render the three-column article fragment for ``page``.

    Args:
        manual: The loaded manual.
        page: The page to render.
        surface: Delivery surface (already re-rooted for the page).

    Returns:
        HTML markup for the fragment.
    """
    rendered = _rendered(manual, page, surface)
    chapter = manual.chapter(page.chapter_slug)
    edit_url = ""
    if manual.edit_base_url and surface.name == "static":
        edit_url = posixpath.join(manual.edit_base_url, page.source_relpath)
    ctx = _common(manual, surface) | {
        "page": page,
        "chapter": chapter,
        "rendered": rendered,
        "body": Markup(rendered.html),  # noqa: S704 — rendered from trusted repo Markdown
        "prev": manual.page(page.prev_key) if page.prev_key else None,
        "next": manual.page(page.next_key) if page.next_key else None,
        "edit_url": edit_url,
    }
    return template_env().get_template("page.html").render(ctx)


def render_index_fragment(manual: Manual, surface: Surface) -> str:
    """Render the manual landing page fragment."""
    paths = []
    for slug, aud in manual.audiences.items():
        chapters = [c for c in manual.chapters if slug in c.audience]
        paths.append({"audience": aud, "chapters": chapters})
    ctx = _common(manual, surface) | {"paths": paths, "page": None, "chapter": None}
    return template_env().get_template("index.html").render(ctx)
