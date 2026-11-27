"""Bind the request's workspace into :mod:`ovid2.workspaces` (adr-workspaces.md).

Every request that resolves a principal also resolves the workspace that
principal works in (:func:`ovid2.web.auth.deps.current_user`). This
middleware does that resolution once, up front, and sets the
:func:`ovid2.workspaces.active_workspace` context variable for the rest of
the request — so services (path resolvers, SQL filters, audit rows) read
the workspace from context instead of threading it through every
signature. Sync endpoints run in a thread pool that copies the context, so
they see the value too.

* Static assets / branding / favicon / login are skipped (no DB hit).
* An unauthenticated request runs in :data:`~ovid2.workspaces.NO_WORKSPACE`
  (the route gate answers ``401`` / redirects anyway).
* An explicitly requested workspace (``X-Ovid2-Workspace``) the principal
  cannot see answers ``404 workspace_not_found`` for ``/api/v1`` — never
  ``403``, so a workspace's existence is not disclosed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ovid2 import workspaces
from ovid2.web.auth import deps
from ovid2.web.request_context import bind_actor, reset_actor

logger = logging.getLogger(__name__)

__all__ = ["WorkspaceMiddleware", "principal_label"]

_SKIP_PREFIXES: tuple[str, ...] = ("/static/", "/branding/", "/favicon")


def principal_label(ctx: deps.AuthContext) -> str | None:
    """Return the audit label of a principal (email, else id; ``None`` when anonymous)."""
    if ctx.anonymous_bypass:
        return None
    if ctx.user_id.startswith(("mcp:", "llm:")):
        return ctx.user_id
    return ctx.email or ctx.user_id


class WorkspaceMiddleware(BaseHTTPMiddleware):
    """Set the active workspace for the lifetime of one request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Resolve the workspace, bind it, and run the request.

        Args:
            request: The inbound request.
            call_next: The downstream ASGI handler.

        Returns:
            The downstream response, or ``404`` for an unknown requested
            workspace on ``/api/v1``.
        """
        path = request.url.path
        if path.startswith(_SKIP_PREFIXES):
            return await call_next(request)
        slug = workspaces.NO_WORKSPACE
        actor: str | None = None
        try:
            ctx = deps.current_user(request)
            if ctx is not None:
                slug = ctx.workspace_id
                actor = principal_label(ctx)
        except HTTPException as exc:
            if exc.status_code == 404 and path.startswith("/api/v1"):
                return JSONResponse({"detail": "workspace_not_found"}, status_code=404)
            # 401/403 from the assistant token etc.: let the route answer.
        except Exception:  # resolution failure must fail closed (no workspace).
            logger.warning("Workspace resolution failed for %s %s.", request.method, path)
        token = workspaces.set_active(slug)
        actor_token = bind_actor(actor)
        try:
            response = await call_next(request)
        finally:
            reset_actor(actor_token)
            workspaces.reset_active(token)
        return response


if __name__ == "__main__":
    raise SystemExit("workspace_middleware is a library module; not runnable directly.")
