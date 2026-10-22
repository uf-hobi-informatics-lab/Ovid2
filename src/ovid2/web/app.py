"""FastAPI application factory.

The ``ovid2 web`` CLI subcommand creates an app instance via
:func:`create_app` and hands it to uvicorn. Import order matters: the
factory pulls in routes, which import services, which import config
modules — none of which touch pipeline code (``ovid2.core``,
``ovid2.transformations``, …).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from ovid2 import __version__
from ovid2.web import db as web_db
from ovid2.web.routes import api_v1, auth, branding, html, llm_html
from ovid2.web.templating import static_dir

logger = logging.getLogger(__name__)


class NoCacheStaticFiles(StaticFiles):
    """``StaticFiles`` that forces revalidation of every asset.

    The live-console UI is a graph of ES modules loaded by static URL
    (``/static/live.js`` → ``display/*/panel.js`` → …) with no
    per-deploy version query. Browsers cache ES modules aggressively, so
    a stale ``panel.js`` can keep rendering the old console long after
    the file on disk changed (the recurring "old checks console" bug).

    Sending ``Cache-Control: no-cache`` makes the browser revalidate on
    every load: unchanged assets return a cheap ``304`` (the ETag /
    Last-Modified handshake is preserved), changed assets are re-fetched
    immediately. Appropriate for an internal tool where correctness of
    the served asset matters more than shaving a round-trip.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _inject_dev_pipelines_dir() -> None:
    """When running from a source checkout, put ``pipelines/`` on ``sys.path``.

    Production installations register pipelines under ``ovid2_pipelines``
    via ``pip install`` and this helper is a no-op. For a developer
    running ``ovid2 web`` from the repo the helper walks up from
    :mod:`ovid2` looking for a sibling ``pipelines/`` directory.
    Overridable via ``OVID2_PIPELINES_DIR``.
    """
    override = os.environ.get("OVID2_PIPELINES_DIR")
    if override:
        candidate = Path(override)
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        return
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pipelines"
        if (candidate / "ovid2_pipelines").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


def create_app() -> FastAPI:
    """Construct the FastAPI app used by ``ovid2 web``.

    Side effects:

    - Ensures the web-tier SQLite tables exist (``web_jobs`` /
      ``web_audit``) via :func:`ovid2.web.db.ensure_schema`.
    - Optionally injects a repo-local ``pipelines/`` directory onto
      ``sys.path`` for dev checkouts (see
      :func:`_inject_dev_pipelines_dir`).
    - Mounts static assets under ``/static``.
    - Registers HTML and JSON routers.
    - Installs :class:`~ovid2.web.auth.middleware.AuthRedirectMiddleware`
      to redirect unauthenticated browser requests to ``/login`` (a no-op
      while ``settings.auth.enabled`` is False).
    """
    _inject_dev_pipelines_dir()
    web_db.ensure_schema()
    app = FastAPI(
        title="Ovid2 control panel",
        version=__version__,
        description=(
            "Web control panel for Ovid2 — configure, submit, and monitor "
            "ETL runs from the browser. The REST API is versioned under "
            "``/api/v1`` and doubles as the surface the ``ovid2 mcp`` "
            "server exposes to LLM agents."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    static_path = static_dir()
    if static_path.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=str(static_path)), name="static")

    app.include_router(llm_html.router)
    app.include_router(html.router)
    app.include_router(api_v1.router)
    app.include_router(branding.router)
    app.include_router(auth.router)

    # Added after the routers, but a ``BaseHTTPMiddleware`` still wraps the
    # whole app and runs *before* routing (Starlette reverse-registration) —
    # do not "fix" this by moving the call above include_router.
    from ovid2.web.auth.middleware import AuthRedirectMiddleware

    app.add_middleware(AuthRedirectMiddleware)
    return app


#: Module-level app instance for ``uvicorn ovid2.web.app:app`` deployments.
app: FastAPI = create_app()
