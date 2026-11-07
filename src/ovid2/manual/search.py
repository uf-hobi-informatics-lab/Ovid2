"""Client-side search index for the manual.

The index is a compact JSON document — one record per page section — that
``manual.js`` loads lazily the first time the search box gains focus and
scores in the browser (title / heading / body term weighting, prefix
matching). No server round-trip per keystroke, and the identical file
powers both the web panel (``/manual/search.json``) and the static site
(``search.json``).
"""

from __future__ import annotations

from typing import Any

from ovid2.manual.loader import Manual
from ovid2.manual.render import LinkResolver, render_page

__all__ = ["build_search_index"]

#: Cap on indexed text per section — keeps the index small without
#: losing recall (the first few hundred words carry the vocabulary).
_MAX_SECTION_CHARS = 1600


def build_search_index(manual: Manual, resolver: LinkResolver) -> dict[str, Any]:
    """Build the search index for ``manual``.

    Args:
        manual: The loaded manual.
        resolver: Surface-specific URL builder (record URLs are absolute
            for the web panel, relative for the static site).

    Returns:
        ``{"v": 1, "docs": [{"t", "h", "c", "u", "x"}, …]}`` where ``t`` is
        the page title, ``h`` the section heading, ``c`` the chapter title,
        ``u`` the URL and ``x`` the section text.
    """
    docs: list[dict[str, str]] = []
    for chapter in manual.chapters:
        for page in chapter.pages:
            rendered = render_page(page, manual, resolver)
            for section in rendered.sections:
                text = section.text
                if not section.anchor and page.summary:
                    text = f"{page.summary} {text}"
                docs.append(
                    {
                        "t": page.title,
                        "h": "" if not section.anchor else section.heading,
                        "c": chapter.title,
                        "u": resolver.page_url(page.key, section.anchor),
                        "x": text[:_MAX_SECTION_CHARS],
                    }
                )
    return {"v": 1, "docs": docs}
