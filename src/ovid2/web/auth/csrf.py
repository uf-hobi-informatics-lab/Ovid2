"""Double-submit CSRF protection for state-changing HTML forms (Wave 3, todo 15).

The session cookie is already issued with ``SameSite=Lax`` (see
:mod:`ovid2.web.auth.cookie`), which blocks the large majority of
cross-site ``POST`` requests a modern browser would make on an attacker's
behalf. This module is **defense-in-depth** on top of that: a stateless,
double-submit CSRF token bound to the app's session signing key, rendered
into every mutating HTML form and re-checked on submit. Even a browser or
proxy that mis-handles ``SameSite`` cannot forge a valid token because the
token's HMAC signature requires the server-side signing key.

Token model
-----------
The token is a timed, signed payload produced by
:class:`itsdangerous.URLSafeTimedSerializer` keyed by the **same** signing
key as the session cookie (:func:`ovid2.web.auth.cookie._resolve_signing_key`).
A distinct salt (:data:`CSRF_SALT`) namespaces CSRF signatures so a session
cookie value can never be replayed as a CSRF token, and vice versa. Because
the token carries only an opaque nonce (not per-request server state), it is
verifiable statelessly: any process holding the signing key can validate a
token another process issued, and tokens survive a restart. Tokens expire
after :data:`CSRF_MAX_AGE_SECONDS`.

Scope of enforcement
---------------------
:func:`require_csrf` is a FastAPI dependency intended for **HTML** mutation
routes (``POST`` / ``PUT`` / ``DELETE`` in :mod:`ovid2.web.routes.html`). It
is a no-op unless ``settings.auth.enabled`` is ``True``. When enforcement is
on, it reads the token from the ``csrf_token`` form field **or** the
``X-CSRF-Token`` request header and rejects the request with
``HTTPException(403)`` when the token is absent or invalid.

``/api/v1/*`` JSON endpoints are intentionally **exempt** from CSRF tokens:
browsers cannot set a custom ``Content-Type: application/json`` header on a
cross-origin request without a CORS preflight the app never approves, so a
content-type gate is sufficient. :func:`require_json_content_type` provides
that gate for API mutations, but it is **not wired into**
:mod:`ovid2.web.routes.api_v1` by this todo — a later pass (or the todo-14
agent gating the REST tier) should attach it.

HTMX
----
HTMX ``hx-post`` requests can carry the token two ways, both accepted by
:func:`require_csrf`:

* a hidden ``<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">``
  inside the posted form, or
* an ``hx-headers`` attribute, e.g.
  ``hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'`` on the element
  (or on ``<body>`` to cover every request).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request

from ovid2.web.auth import cookie
from ovid2.web.services import settings_svc

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

__all__ = [
    "CSRF_FIELD_NAME",
    "CSRF_HEADER_NAME",
    "CSRF_MAX_AGE_SECONDS",
    "CSRF_SALT",
    "issue_token",
    "register_csrf",
    "require_csrf",
    "require_json_content_type",
    "validate_token",
]

#: Salt namespacing CSRF signatures away from the session-cookie serializer,
#: which is keyed by the same signing key.
CSRF_SALT: str = "ovid2.web.auth.csrf.v1"

#: Opaque payload signed into every token. The value is irrelevant — only the
#: signature and freshness matter for a double-submit token — so a constant
#: keeps the tokens compact.
_CSRF_PAYLOAD: str = "csrf"

#: How long an issued token stays valid. Long enough to fill in and submit a
#: form (including a slow multi-field settings page), short enough to bound
#: replay.
CSRF_MAX_AGE_SECONDS: int = 8 * 60 * 60

#: Form field the token is rendered into.
CSRF_FIELD_NAME: str = "csrf_token"

#: Request header the token may alternatively be sent in (HTMX / fetch).
CSRF_HEADER_NAME: str = "X-CSRF-Token"

#: Mutating HTTP methods CSRF applies to.
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _serializer() -> URLSafeTimedSerializer:
    """Return a serializer keyed by the shared session signing key.

    Reuses :func:`ovid2.web.auth.cookie._resolve_signing_key` so CSRF tokens
    and session cookies share the app's single on-disk secret. A distinct
    :data:`CSRF_SALT` keeps the two signature domains disjoint.

    Returns:
        A timed URL-safe serializer for CSRF tokens.
    """
    return URLSafeTimedSerializer(cookie._resolve_signing_key(), salt=CSRF_SALT)


def issue_token() -> str:
    """Return a fresh, signed CSRF token.

    The token is a timed, signed opaque payload. It is verifiable by
    :func:`validate_token` without any server-side per-request state, so a
    template can render one at page-render time and a later request can
    validate it in a different worker process.

    Returns:
        A URL-safe CSRF token string suitable for a hidden form field or the
        ``X-CSRF-Token`` header.
    """
    return _serializer().dumps(_CSRF_PAYLOAD)


def validate_token(token: str | None) -> bool:
    """Return whether ``token`` is a valid, unexpired token from :func:`issue_token`.

    Args:
        token: The candidate token, or ``None`` when absent.

    Returns:
        ``True`` iff the token verifies against the session signing key and is
        no older than :data:`CSRF_MAX_AGE_SECONDS`; ``False`` otherwise
        (absent, tampered, expired, or malformed).
    """
    if not token:
        return False
    try:
        value = _serializer().loads(token, max_age=CSRF_MAX_AGE_SECONDS)
    except SignatureExpired:
        logger.debug("CSRF token expired.")
        return False
    except BadSignature:
        logger.debug("CSRF token failed signature verification.")
        return False
    return bool(value == _CSRF_PAYLOAD)


def _auth_enabled() -> bool:
    """Return whether auth (and therefore CSRF enforcement) is enabled."""
    return bool(settings_svc.get_settings().auth.enabled)


async def _read_token(request: Request) -> str | None:
    """Return the CSRF token from the form field or header, or ``None``.

    The header (:data:`CSRF_HEADER_NAME`) is checked first so HTMX requests
    that set ``hx-headers`` do not need to parse the body. When absent, the
    request's form body is read and the :data:`CSRF_FIELD_NAME` field is used.

    Args:
        request: The inbound request.

    Returns:
        The token string, or ``None`` when neither source carries one.
    """
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return header_token
    try:
        form = await request.form()
    except (AssertionError, ValueError):
        # No parseable form body (e.g. wrong content-type) — nothing to read.
        return None
    raw = form.get(CSRF_FIELD_NAME)
    return raw if isinstance(raw, str) else None


async def require_csrf(request: Request) -> None:
    """FastAPI dependency enforcing CSRF on HTML mutation routes.

    A no-op when ``settings.auth.enabled`` is ``False`` (preserves today's
    open behavior) or when the request method is not mutating. For mutating
    HTML routes with auth enabled, reads the token via :func:`_read_token`
    and raises ``HTTPException(403)`` when it is absent or invalid.

    ``/api/v1/*`` requests are skipped here — those are protected by
    :func:`require_json_content_type` instead (see the module docstring).

    Args:
        request: The inbound request.

    Raises:
        HTTPException: With status 403 when the token is absent or invalid.
    """
    if not _auth_enabled():
        return
    if request.method.upper() not in _MUTATING_METHODS:
        return
    # API routes are exempt from token-based CSRF (content-type gate instead).
    if request.url.path.startswith("/api/v1/"):
        return
    token = await _read_token(request)
    if not validate_token(token):
        logger.warning(
            "Rejected %s %s: missing or invalid CSRF token.",
            request.method,
            request.url.path,
        )
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


async def require_json_content_type(request: Request) -> None:
    """FastAPI dependency requiring a JSON content-type on API mutations.

    This is the CSRF countermeasure for ``/api/v1/*`` mutating endpoints: a
    browser cannot set ``Content-Type: application/json`` on a cross-origin
    request without a CORS preflight the app never approves, so requiring it
    blocks form-based cross-site POSTs while leaving legitimate JSON clients
    (the panel's own fetch calls, the MCP tier) unaffected.

    A no-op when ``settings.auth.enabled`` is ``False`` or when the method is
    not mutating.

    .. note::
        This helper is **not yet wired** into
        :mod:`ovid2.web.routes.api_v1`. It is provided for a later pass (or
        the REST-gating todo) to attach as a router-level dependency.

    Args:
        request: The inbound request.

    Raises:
        HTTPException: With status 415 when a mutating API request does not
            declare a JSON content-type.
    """
    if not _auth_enabled():
        return
    if request.method.upper() not in _MUTATING_METHODS:
        return
    content_type = request.headers.get("content-type", "")
    # Strip any ``; charset=...`` parameter before comparing.
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="Mutating API requests must use Content-Type: application/json.",
        )


def register_csrf(templates: Jinja2Templates) -> None:
    """Register the ``csrf_token`` Jinja global on ``templates``.

    After this call, templates can render ``{{ csrf_token() }}`` into a
    hidden form field or an ``hx-headers`` attribute. The callable returns a
    freshly-issued token on every render.

    Args:
        templates: The Jinja2 templates environment to augment.
    """
    templates.env.globals[CSRF_FIELD_NAME] = issue_token
