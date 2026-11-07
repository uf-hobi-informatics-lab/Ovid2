"""``/manual`` — the Ovid2 Manual inside the control panel.

Serves the same Markdown pages the static site is built from
(:mod:`ovid2.manual`), wrapped in the panel shell so the manual follows
the reader's appearance theme and keeps the nav, command palette and
assistant at hand.

Routes (all ``include_in_schema=False`` — HTML/asset surfaces, not API):

- ``GET /manual`` — landing page (audience paths + chapter index).
- ``GET /manual/search.json`` — client-side search index.
- ``GET /manual/assets/{path}`` — diagrams / images referenced by pages.
- ``GET /manual/{chapter}`` — redirects to the chapter's first page.
- ``GET /manual/{chapter}/{page}`` — one page.

Gate: ``Capability.VIEW_PUBLIC`` — every activated user (guest and up).
With auth disabled the synthetic admin passes; with auth enabled the
:class:`~ovid2.web.auth.middleware.AuthRedirectMiddleware` sends anonymous
browsers to ``/login`` first.

The loaded manual is cached for the process lifetime; set
``OVID2_MANUAL_RELOAD=1`` while authoring to re-read sources per request.
"""

from __future__ import annotations

import mimetypes
import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from markupsafe import Markup

from ovid2.manual import Manual, load_manual
from ovid2.manual.layout import (
    SEARCH_SURFACE,
    WEB_SURFACE,
    render_index_fragment,
    render_page_fragment,
)
from ovid2.manual.search import build_search_index
from ovid2.web.auth.deps import require_capability
from ovid2.web.auth.roles import Capability
from ovid2.web.templating import templates

__all__ = ["router"]

router = APIRouter(
    dependencies=[Depends(require_capability(Capability.VIEW_PUBLIC))],
)


@lru_cache(maxsize=1)
def _cached_manual() -> Manual:
    return load_manual()


def _manual() -> Manual:
    """Return the loaded manual (re-read per request in authoring mode)."""
    if os.environ.get("OVID2_MANUAL_RELOAD") == "1":
        return load_manual()
    return _cached_manual()


@lru_cache(maxsize=1)
def _cached_index() -> dict[str, object]:
    return build_search_index(_cached_manual(), SEARCH_SURFACE)


@router.get("/manual", response_class=HTMLResponse, include_in_schema=False)
async def manual_home(request: Request) -> HTMLResponse:
    """Render the manual landing page."""
    manual = _manual()
    return templates.TemplateResponse(
        request,
        "manual.html",
        {
            "title": manual.title,
            "fragment": Markup(render_index_fragment(manual, WEB_SURFACE)),  # noqa: S704
        },
    )


@router.get("/manual/search.json", include_in_schema=False)
async def manual_search_index() -> JSONResponse:
    """Return the client-side search index."""
    if os.environ.get("OVID2_MANUAL_RELOAD") == "1":
        data: dict[str, object] = build_search_index(_manual(), SEARCH_SURFACE)
    else:
        data = _cached_index()
    return JSONResponse(data, headers={"Cache-Control": "no-cache"})


@router.get("/manual/assets/{relpath:path}", include_in_schema=False)
async def manual_asset(relpath: str) -> FileResponse:
    """Serve a manual asset, refusing anything outside ``assets/``."""
    asset_dir = _manual().asset_dir.resolve()
    target = (asset_dir / relpath).resolve()
    if not target.is_file() or asset_dir not in target.parents:
        raise HTTPException(status_code=404)
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type or "application/octet-stream")


@router.get("/manual/{chapter_slug}", include_in_schema=False)
async def manual_chapter(chapter_slug: str) -> RedirectResponse:
    """Redirect a bare chapter URL to the chapter's landing page."""
    chapter = _manual().chapter(chapter_slug)
    if chapter is None:
        raise HTTPException(status_code=404, detail="No such manual chapter")
    return RedirectResponse(WEB_SURFACE.page_url(chapter.landing.key), status_code=307)


@router.get(
    "/manual/{chapter_slug}/{page_slug}", response_class=HTMLResponse, include_in_schema=False
)
async def manual_page(request: Request, chapter_slug: str, page_slug: str) -> HTMLResponse:
    """Render one manual page."""
    manual = _manual()
    page = manual.page(f"{chapter_slug}/{page_slug}")
    if page is None:
        raise HTTPException(status_code=404, detail="No such manual page")
    return templates.TemplateResponse(
        request,
        "manual.html",
        {
            "title": f"{page.title} · {manual.title}",
            "fragment": Markup(render_page_fragment(manual, page, WEB_SURFACE)),  # noqa: S704
        },
    )
