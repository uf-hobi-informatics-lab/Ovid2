"""Signed session-cookie manager (Wave 2, todo 8).

Wraps :class:`itsdangerous.URLSafeTimedSerializer` to turn an opaque
server-side session id into a tamper-evident, time-limited cookie value
and back again. The cookie carries **only** the signed session id — never
user identity, role, or any IdP token. Identity and authorization are
resolved server-side on every request by looking the session id up in
``web_sessions`` (see ``sessions_svc``); this module knows nothing about
who the session belongs to.

Signing key
-----------
The HMAC signing key is persisted as a JSON secret named ``auth_session``
with the single key ``signing_key`` under
``$XDG_CONFIG_HOME/ovid2/secrets.d/`` (mode ``0o600``, enforced by
:mod:`ovid2.web.services.secrets_svc`). On first use — when no such secret
exists — a fresh 256-bit URL-safe key is generated
(:func:`secrets.token_urlsafe(32)`) and written via
:func:`secrets_svc.write_secret`. Because the key lives on disk, the
serializer's signatures survive a process restart, so already-issued
session cookies keep verifying across restarts (a restart does not log
everybody out).

The key is read back through :func:`ovid2.config.secrets.load_secret`
(``secrets_svc`` never returns secret *values*, only key names), and the
resolved value is cached in-process for the lifetime of the interpreter.

Cookie attributes
-----------------
``issue_cookie`` always sets ``HttpOnly`` and ``SameSite=Lax``. The
``Secure`` flag is set when the request is served over HTTPS — either the
request's own scheme is ``https`` (direct TLS) or, when
``settings.auth.trust_forwarded_proto`` is enabled, the shared reverse
proxy forwarded ``X-Forwarded-Proto: https`` (the deployment model
documented in
the 2026-08-24-web-tls-via-shared-reverse-proxy design note).
The cookie's ``Max-Age`` is the absolute session TTL from
``settings.auth.absolute_ttl_minutes``.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import threading
from typing import TYPE_CHECKING

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ovid2.config.secrets import SecretNotFoundError, load_secret
from ovid2.web.models import SecretWrite
from ovid2.web.services import secrets_svc, settings_svc

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

__all__ = [
    "COOKIE_PATH",
    "SESSION_SALT",
    "SIGNING_KEY_SECRET_KEY",
    "SIGNING_KEY_SECRET_NAME",
    "clear_cookie",
    "issue_cookie",
    "read_session_id",
    "reset_signing_key_cache",
]

#: Logical secret name (``secrets.d/auth_session.json``).
SIGNING_KEY_SECRET_NAME: str = "auth_session"  # noqa: S105 — secret file name, not a credential

#: JSON key inside that secret file holding the raw signing key.
SIGNING_KEY_SECRET_KEY: str = "signing_key"  # noqa: S105 — JSON field name, not a credential

#: Namespacing salt so this serializer's signatures cannot be confused
#: with any other :class:`URLSafeTimedSerializer` keyed by the same secret.
SESSION_SALT: str = "ovid2.web.auth.session-cookie.v1"

#: Cookie path — the whole app is under ``/``.
COOKIE_PATH: str = "/"


_SIGNING_KEY_LOCK = threading.Lock()
_SIGNING_KEY_CACHE: str | None = None


def _generate_signing_key() -> str:
    """Return a fresh 256-bit URL-safe signing key."""
    return _secrets.token_urlsafe(32)


def _resolve_signing_key() -> str:
    """Return the persisted signing key, generating + persisting on first use.

    The key is read from the ``auth_session`` secret. When that secret does
    not exist yet, a new 256-bit key is generated, persisted via
    :func:`secrets_svc.write_secret` (mode ``0o600``), and read back through
    :func:`ovid2.config.secrets.load_secret`. The resolved value is cached
    in-process so subsequent calls do not touch disk.

    Returns:
        The raw signing key string.

    Raises:
        RuntimeError: If the key cannot be read back after being written,
            or if a persisted secret exists but lacks a usable
            ``signing_key`` value.
    """
    global _SIGNING_KEY_CACHE
    with _SIGNING_KEY_LOCK:
        if _SIGNING_KEY_CACHE is not None:
            return _SIGNING_KEY_CACHE

        try:
            payload = load_secret(SIGNING_KEY_SECRET_NAME)
        except SecretNotFoundError:
            payload = None

        key: str | None = None
        if payload is not None:
            raw = payload.get(SIGNING_KEY_SECRET_KEY)
            if isinstance(raw, str) and raw:
                key = raw

        if key is None:
            key = _generate_signing_key()
            secrets_svc.write_secret(
                SIGNING_KEY_SECRET_NAME,
                SecretWrite(values={SIGNING_KEY_SECRET_KEY: key}),
                actor="auth.cookie",
            )
            # Read the value back through the canonical loader so we honour
            # the same permission checks the rest of the app uses.
            reloaded = load_secret(SIGNING_KEY_SECRET_NAME)
            raw = reloaded.get(SIGNING_KEY_SECRET_KEY)
            if not isinstance(raw, str) or not raw:
                raise RuntimeError(
                    "Session signing key was written but could not be read back."
                )
            key = raw

        _SIGNING_KEY_CACHE = key
        return key


def reset_signing_key_cache() -> None:
    """Drop the in-process signing-key cache (used by tests)."""
    global _SIGNING_KEY_CACHE
    with _SIGNING_KEY_LOCK:
        _SIGNING_KEY_CACHE = None


def _serializer() -> URLSafeTimedSerializer:
    """Return a serializer keyed by the resolved signing key."""
    return URLSafeTimedSerializer(_resolve_signing_key(), salt=SESSION_SALT)


def _absolute_ttl_seconds() -> int:
    """Return the absolute session TTL in seconds from settings."""
    settings = settings_svc.get_settings()
    return int(settings.auth.absolute_ttl_minutes) * 60


def _is_secure_request(request: Request | None) -> bool:
    """Return whether the request should be treated as HTTPS.

    ``True`` when the request's own scheme is ``https`` or — when
    ``settings.auth.trust_forwarded_proto`` is enabled — the reverse proxy
    forwarded ``X-Forwarded-Proto: https``. Only the first (left-most)
    value of the header is considered, matching proxy convention.

    Args:
        request: The inbound request, or ``None`` when unavailable.

    Returns:
        ``True`` if the cookie should carry the ``Secure`` attribute.
    """
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    settings = settings_svc.get_settings()
    if not settings.auth.trust_forwarded_proto:
        return False
    forwarded = request.headers.get("x-forwarded-proto")
    if not forwarded:
        return False
    first = forwarded.split(",")[0].strip().lower()
    return first == "https"


def issue_cookie(
    response: Response,
    session_id: str,
    request: Request | None = None,
) -> None:
    """Set the signed session cookie on ``response``.

    The cookie value is ``session_id`` signed with the timed serializer.
    Attributes: ``HttpOnly`` and ``SameSite=Lax`` are always set;
    ``Secure`` is set when :func:`_is_secure_request` returns ``True``;
    ``Max-Age`` is the absolute session TTL.

    Args:
        response: The Starlette/FastAPI response to mutate.
        session_id: The opaque server-side session id to sign and carry.
        request: The inbound request, used to decide the ``Secure`` flag.
    """
    settings = settings_svc.get_settings()
    signed = _serializer().dumps(session_id)
    response.set_cookie(
        key=settings.auth.cookie_name,
        value=signed,
        max_age=_absolute_ttl_seconds(),
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
    )


def read_session_id(request: Request) -> str | None:
    """Return the verified session id from the request cookie, or ``None``.

    Reads the cookie named ``settings.auth.cookie_name``, verifies its
    signature, and enforces the absolute-TTL age limit. Returns ``None``
    when the cookie is absent, tampered with, or older than the absolute
    TTL.

    Args:
        request: The inbound request carrying the cookie.

    Returns:
        The original session id, or ``None`` if it cannot be trusted.
    """
    settings = settings_svc.get_settings()
    raw = request.cookies.get(settings.auth.cookie_name)
    if not raw:
        return None
    try:
        value = _serializer().loads(raw, max_age=_absolute_ttl_seconds())
    except SignatureExpired:
        logger.debug("Session cookie expired.")
        return None
    except BadSignature:
        logger.debug("Session cookie failed signature verification.")
        return None
    if not isinstance(value, str):
        return None
    return value


def clear_cookie(response: Response) -> None:
    """Delete the session cookie (same key and path as :func:`issue_cookie`).

    Args:
        response: The Starlette/FastAPI response to mutate.
    """
    settings = settings_svc.get_settings()
    response.delete_cookie(key=settings.auth.cookie_name, path=COOKIE_PATH)
