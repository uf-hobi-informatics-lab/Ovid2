"""FastAPI route modules.

Split by concern: HTML routes render Jinja2 templates and HTMX fragments;
API routes live in :mod:`ovid2.web.routes.api_v1` and emit JSON.

Every route module exposes an ``APIRouter`` named ``router`` that
:mod:`ovid2.web.app` mounts.
"""
