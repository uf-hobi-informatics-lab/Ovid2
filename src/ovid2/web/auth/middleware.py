"""HTML auth redirect middleware (Wave 3, todo 13).

This module owns the *browser-facing* half of the authentication gate. The
REST tier (``/api/v1/*``) is gated by per-route capability dependencies
(:func:`ovid2.web.auth.deps.require_capability`) that answer JSON ``401`` /
``403``; those requests are deliberately passed through here untouched. Every
*other* path is an HTML page a human visits, and an unauthenticated human
should land on ``/login`` — not on a raw ``401`` — so this middleware issues a
``302`` redirect to ``/login?next=<original path+query>`` for them.

Behaviour summary
-----------------
The middleware is a **total no-op** when ``settings.auth.enabled`` is
``False`` — no session lookup, no redirect, byte-identical to today's
open-access panel. When auth is enabled:

* An allow-list of paths (:data:`_ALLOWLIST_EXACT` / :data:`_ALLOWLIST_PREFIXES`
  / :func:`_is_favicon`) is always reachable without a session so the login
  flow, static assets, branding, liveness probe, and favicon can render for an
  anonymous visitor.
* ``/api/v1/*`` requests are never redirected — they flow to the route
  dependencies which return JSON status codes.
* For any other path, the current user is resolved via
  :func:`ovid2.web.auth.deps.current_user`:

  - ``None`` (no / untrusted session) → ``302`` to ``/login?next=…``.
  - A ``pending`` / ``disabled`` principal → the "pending approval" page
    (HTTP ``200``) rather than a redirect, so the visitor is not trapped in a
    redirect loop (they *have* a valid session, they just lack an approved
    role).
  - An approved principal → the request proceeds untouched.

Fail-safe
---------
Authentication is a security boundary, so an *unexpected* failure while
resolving the user must fail **closed**: the visitor is redirected to
``/login`` rather than being let through. The broad guard names
:class:`Exception` (never a bare ``except``) and logs at ``warning`` so an
operator can see repeated resolution failures.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ovid2.web.auth import deps
from ovid2.web.auth.roles import Role
from ovid2.web.services import settings_svc, users_svc

logger = logging.getLogger(__name__)

__all__ = ["AuthRedirectMiddleware"]

#: Exact paths reachable without a session. ``/login`` and ``/health`` are the
#: bare pages (the login form and the HTML liveness page); ``/logout`` must
#: always be reachable so a pending / break-glass principal (whose session has
#: no resolvable user row) can still sign out instead of being trapped.
#: Everything with sub-paths is covered by :data:`_ALLOWLIST_PREFIXES`.
_ALLOWLIST_EXACT: frozenset[str] = frozenset({"/login", "/logout", "/health"})

#: Path prefixes reachable without a session. ``/login/`` covers the provider
#: redirect + the break-glass POST; ``/auth/`` covers callback / recheck /
#: front-channel logout; ``/static/`` + ``/branding/`` are unauthenticated
#: assets.
_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "/login/",
    "/auth/",
    "/static/",
    "/branding/",
)

#: Prefix identifying REST requests, which are gated by route dependencies and
#: must never be redirected by this middleware.
_API_PREFIX: str = "/api/v1"

#: HTTP methods treated as browser navigations. Only these are redirected to
#: the login page; mutating methods fall through to their route's own gates
#: (see :meth:`AuthRedirectMiddleware.dispatch`).
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def _is_favicon(path: str) -> bool:
    """Return whether ``path`` is a favicon request.

    Browsers request ``/favicon.ico`` (and occasionally ``/favicon-32.png``)
    without user intent; these must resolve for an anonymous visitor.

    Args:
        path: The request URL path.

    Returns:
        ``True`` when the path is the exact favicon request or begins with
        ``/favicon``.
    """
    return path == "/favicon.ico" or path.startswith("/favicon")


def _is_allowlisted(path: str) -> bool:
    """Return whether ``path`` may be reached without authentication.

    Args:
        path: The request URL path.

    Returns:
        ``True`` for login / auth / static / branding / liveness / favicon
        paths, ``False`` otherwise.
    """
    if path in _ALLOWLIST_EXACT:
        return True
    if _is_favicon(path):
        return True
    return any(path.startswith(prefix) for prefix in _ALLOWLIST_PREFIXES)


def _login_redirect(request: Request) -> RedirectResponse:
    """Build the ``302`` redirect to the login page for ``request``.

    The ``next`` param is the URL-escaped **local** path + query of the
    current request. The login route re-validates ``next`` server-side
    (``routes.auth._safe_next``), so an attacker cannot smuggle an
    off-site redirect through it; passing the current request path is safe.

    Args:
        request: The inbound request being denied.

    Returns:
        A ``302`` :class:`~starlette.responses.RedirectResponse` to
        ``/login?next=<escaped path+query>``.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    next_param = quote(target, safe="")
    return RedirectResponse(url=f"/login?next={next_param}", status_code=302)


def _is_pending_or_disabled(ctx: deps.AuthContext) -> bool:
    """Return whether the resolved principal is pending or disabled.

    A ``pending`` user maps to :data:`~ovid2.web.auth.roles.Role.PENDING`
    (empty capability set) in :func:`ovid2.web.auth.deps.current_user`, so
    that case is detectable from ``ctx.role`` alone. A ``disabled`` user keeps
    their previously-assigned role, so we consult the durable user record's
    ``status`` field for them. The record lookup is wrapped defensively: any
    failure degrades to "not pending/disabled" so a transient DB hiccup cannot
    wrongly trap an approved user on the pending page (they will still be
    gated by the route dependencies if truly unauthorised).

    Args:
        ctx: The resolved :class:`~ovid2.web.auth.deps.AuthContext`.

    Returns:
        ``True`` when the principal should see the pending-approval page.
    """
    if ctx.role == Role.PENDING:
        return True
    if ctx.anonymous_bypass:
        return False
    try:
        record = users_svc.get_user(ctx.user_id)
    except Exception:  # a lookup failure must not trap a user out of the panel.
        logger.warning(
            "Failed to read user record for %r during pending check.",
            ctx.user_id,
        )
        return False
    if record is None:
        return False
    return record.get("status") == "disabled"


class AuthRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated browser requests to ``/login``.

    Registered in :func:`ovid2.web.app.create_app` after the routers.
    Starlette runs :class:`~starlette.middleware.base.BaseHTTPMiddleware`
    wrappers before routing, so this gate sees every request. It is a no-op
    while auth is disabled and never touches ``/api/v1/*`` requests.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Gate one request, redirecting or rendering as the rules require.

        Args:
            request: The inbound request.
            call_next: The downstream ASGI handler (routing).

        Returns:
            The downstream response when the request is allowed through, a
            ``302`` redirect to ``/login`` when unauthenticated, or the
            pending-approval page when the principal is pending / disabled.
        """
        try:
            enabled = settings_svc.get_settings().auth.enabled
        except Exception:  # a settings read failure must fail safe.
            logger.warning("Failed to read auth settings; treating as disabled.")
            enabled = False

        if not enabled:
            return await call_next(request)

        path = request.url.path
        # REST is gated by route dependencies (JSON 401/403); never redirect.
        if path == _API_PREFIX or path.startswith(f"{_API_PREFIX}/"):
            return await call_next(request)
        # Login / auth / assets / liveness / favicon are always reachable.
        if _is_allowlisted(path):
            return await call_next(request)
        # Only browser *navigations* (safe methods) are redirected to the
        # login page — 302-redirecting a POST/PUT/DELETE would discard its
        # body and send an XHR/form submit to a GET page. Mutating requests
        # fall through to their route, where the CSRF dependency (a signed
        # token an unauthenticated visitor cannot obtain, since it is only
        # rendered into authenticated pages) and any capability gate reject
        # them. This also keeps the double-submit CSRF contract intact.
        if request.method.upper() not in _SAFE_METHODS:
            return await call_next(request)

        try:
            ctx = deps.current_user(request)
        except Exception:  # a resolution failure must fail safe (redirect to login).
            logger.warning(
                "current_user raised while gating %s %s; redirecting to /login.",
                request.method,
                path,
            )
            return _login_redirect(request)

        if ctx is None:
            return _login_redirect(request)

        if _is_pending_or_disabled(ctx):
            return self._render_pending(request, ctx)

        return await call_next(request)

    @staticmethod
    def _render_pending(request: Request, ctx: deps.AuthContext) -> Response:
        """Render the pending-approval page (HTTP ``200``).

        Imported lazily so the middleware module does not pull the templating
        stack (and its lazy CSRF-global registration) at import time.

        Args:
            request: The inbound request.
            ctx: The resolved principal awaiting approval.

        Returns:
            The rendered ``pending.html`` page with a ``200`` status.
        """
        from ovid2.web.templating import templates

        return templates.TemplateResponse(
            request,
            "pending.html",
            {"email": ctx.email},
            status_code=200,
        )


if __name__ == "__main__":
    raise SystemExit("middleware is a library module; not runnable directly.")
