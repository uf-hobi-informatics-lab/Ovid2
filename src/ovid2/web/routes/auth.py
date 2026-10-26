"""OIDC + break-glass authentication routes (Wave 3, todos 11 + 12).

This router owns every *surface* of the login lifecycle:

* ``GET  /login``                     — the login page (provider buttons +
                                          break-glass form).
* ``GET  /login/{provider}``          — start the OIDC authorization-code
                                          flow (Authlib ``authorize_redirect``).
* ``GET  /auth/callback/{provider}``  — finish the flow, mint a server-side
                                          session, and issue the signed cookie.
* ``POST /login/breakglass``          — sealed local-admin fallback login.
* ``GET  /logout``                    — revoke the session + clear the cookie.
* ``GET  /auth/recheck``              — cheap JSON liveness probe for the
                                          silent-recheck script (todo 12).
* ``GET  /auth/frontchannel-logout``  — Microsoft front-channel logout URL
                                          (todo 12).

Every route is ``include_in_schema=False`` — none of them belong in the
public OpenAPI surface the MCP server mirrors.

Security posture
----------------
* The ``next`` redirect target is always run through :func:`_safe_next`,
  which accepts only a **local** path (``/foo``) and rejects protocol-
  relative (``//evil.com``), absolute-URL (``http(s)://…``), and
  backslash-obfuscated targets — defaulting to ``/``.
* The OIDC ``state`` + ``nonce`` (and the validated ``next``) are carried
  across the redirect in a **short-lived signed transient cookie**
  (itsdangerous, a dedicated salt) — never in a query string and never
  logged. A callback whose ``state`` / ``nonce`` do not match the value
  stashed at authorize time is rejected with ``400`` and mints no session.
* No token — ID token, access token, or client secret — is ever logged.

This module is part of the web tier and imports **no** pipeline module
(``ovid2.core`` / ``ovid2.transformations`` / ``ovid2.formatters`` /
``ovid2.mappers`` / ``pipelines``).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ovid2.web import db as web_db
from ovid2.web.auth import breakglass, cookie, deps, oidc
from ovid2.web.auth.roles import Role
from ovid2.web.models import SettingsAuth
from ovid2.web.services import sessions_svc, settings_svc, users_svc
from ovid2.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router"]

#: ``user_id`` stamped on the synthetic break-glass principal's session.
BREAKGLASS_USER_ID: str = "breakglass"

#: Synthetic issuer recorded for the break-glass principal (never an IdP).
_BREAKGLASS_ISSUER: str = "ovid2:breakglass"

#: Namespacing salt for the transient authorize-flow cookie. Distinct from
#: the session-cookie salt so the two signatures can never be confused.
_TRANSIENT_SALT: str = "ovid2.web.auth.oidc-transient.v1"

#: Name of the short-lived cookie holding the pending authorize-flow data.
_TRANSIENT_COOKIE: str = "ovid2_oidc_txn"

#: Lifetime (seconds) of the transient authorize-flow cookie — long enough
#: to complete an interactive login, short enough to bound replay.
_TRANSIENT_MAX_AGE: int = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_next(next_value: str | None) -> str:
    """Return a safe local redirect target, defaulting to ``/``.

    Accepts only a **local** path that starts with a single ``/`` and is
    not protocol-relative. Rejects (falling back to ``/``):

    * ``None`` / empty / whitespace.
    * Absolute URLs (``http://…``, ``https://…``, any ``scheme:``).
    * Protocol-relative URLs (``//evil.com``).
    * Backslash-obfuscated targets (``/\\evil.com``, ``\\evil.com``) that
      some browsers normalise to a network path.

    Args:
        next_value: The candidate redirect target (usually a query param).

    Returns:
        The validated local path, or ``/`` when the candidate is unsafe.
    """
    if not next_value:
        return "/"
    candidate = next_value.strip()
    if not candidate:
        return "/"
    # Normalise backslashes — some browsers treat ``\`` like ``/``.
    normalised = candidate.replace("\\", "/")
    # Must be an absolute *local* path.
    if not normalised.startswith("/"):
        return "/"
    # Reject protocol-relative ("//host") targets.
    if normalised.startswith("//"):
        return "/"
    # Reject anything the URL parser reads as having a scheme or netloc.
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return "/"
    return candidate


def _transient_serializer() -> URLSafeTimedSerializer:
    """Return a serializer for the transient authorize-flow cookie.

    Reuses the same on-disk signing key as the session cookie (via
    :func:`ovid2.web.auth.cookie._resolve_signing_key`) but under a
    distinct salt, so a transient-cookie signature can never be replayed
    as a session cookie or vice-versa.
    """
    return URLSafeTimedSerializer(
        cookie._resolve_signing_key(),
        salt=_TRANSIENT_SALT,
    )


def _stash_transient(response: Response, request: Request, payload: dict[str, Any]) -> None:
    """Set the signed transient authorize-flow cookie on ``response``.

    Args:
        response: The response to mutate.
        request: The inbound request (used to decide the ``Secure`` flag).
        payload: The flow data to sign and carry (``state`` / ``nonce`` /
            ``next`` / Authlib session bits). Never contains a token.
    """
    signed = _transient_serializer().dumps(payload)
    response.set_cookie(
        key=_TRANSIENT_COOKIE,
        value=signed,
        max_age=_TRANSIENT_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie._is_secure_request(request),
    )


def _read_transient(request: Request) -> dict[str, Any] | None:
    """Return the verified transient authorize-flow payload, or ``None``.

    Args:
        request: The inbound callback request carrying the cookie.

    Returns:
        The stashed flow data, or ``None`` when the cookie is absent,
        tampered with, or older than :data:`_TRANSIENT_MAX_AGE`.
    """
    raw = request.cookies.get(_TRANSIENT_COOKIE)
    if not raw:
        return None
    try:
        value = _transient_serializer().loads(raw, max_age=_TRANSIENT_MAX_AGE)
    except SignatureExpired:
        logger.debug("OIDC transient cookie expired.")
        return None
    except BadSignature:
        logger.debug("OIDC transient cookie failed signature verification.")
        return None
    if not isinstance(value, dict):
        return None
    return value


def _clear_transient(response: Response) -> None:
    """Delete the transient authorize-flow cookie."""
    response.delete_cookie(key=_TRANSIENT_COOKIE, path="/")


def _is_authed(request: Request) -> bool:
    """Return whether ``request`` carries a valid, non-pending session.

    Resolution order:

    1. The synthetic **break-glass** principal — a valid session whose
       ``user_id`` is :data:`BREAKGLASS_USER_ID` — counts as authenticated
       (it has no ``web_users`` row, so :func:`deps.current_user` would
       reject it; we recognise it here instead).
    2. Otherwise :func:`deps.current_user`. A
       :data:`~ovid2.web.auth.roles.Role.PENDING` principal is treated as
       *not* usefully authenticated for the login page and the silent-
       recheck probe (they have a session but no capabilities).

    Never raises — any failure is treated as "not authenticated" so a probe
    endpoint can stay 200/JSON.

    Args:
        request: The inbound request.

    Returns:
        ``True`` when a usable principal resolves, else ``False``.
    """
    try:
        session_id = cookie.read_session_id(request)
        if session_id is not None:
            session = sessions_svc.get_valid(session_id)
            if session is not None and session.user_id == BREAKGLASS_USER_ID:
                return True
        ctx = deps.current_user(request)
    except Exception:
        logger.debug("current_user resolution failed during auth check.", exc_info=True)
        return False
    if ctx is None:
        return False
    return ctx.role != Role.PENDING


def _idp_sid_from_token(token: dict[str, Any]) -> str | None:
    """Extract the IdP session id (``sid``) from a parsed token, if present.

    The ``sid`` claim links a server-side session to the IdP session so a
    front-channel logout can revoke it. Looks in the parsed ``userinfo``
    (ID-token claims) first, then the top-level token dict.

    Args:
        token: The token dict returned by ``authorize_access_token``.

    Returns:
        The ``sid`` string, or ``None`` when absent.
    """
    userinfo = token.get("userinfo")
    if isinstance(userinfo, dict):
        sid = userinfo.get("sid")
        if isinstance(sid, str) and sid:
            return sid
    sid = token.get("sid")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _claims_from_token(token: dict[str, Any]) -> dict[str, Any]:
    """Return the ID-token claims dict from a parsed token.

    Args:
        token: The token dict returned by ``authorize_access_token``.

    Returns:
        The ``userinfo`` claims dict, or an empty dict when absent.
    """
    userinfo = token.get("userinfo")
    return userinfo if isinstance(userinfo, dict) else {}


async def _end_session_endpoint(provider_key: str) -> str | None:
    """Return the IdP ``end_session_endpoint`` for a provider, if discoverable.

    Best-effort: resolves the provider's Authlib client and reads its
    server metadata. Any failure (unknown provider, discovery error,
    missing key) yields ``None`` so :func:`logout` falls back to
    ``/login``.

    Args:
        provider_key: The provider key from ``settings.auth.providers``.

    Returns:
        The end-session endpoint URL, or ``None``.
    """
    settings = settings_svc.get_settings()
    try:
        registry = oidc.get_registry(settings.auth)
        client = registry.create_client(provider_key)
        if client is None:
            return None
        metadata = await client.load_server_metadata()
    except Exception:
        logger.debug(
            "Could not resolve end_session_endpoint for provider %r.",
            provider_key,
            exc_info=True,
        )
        return None
    endpoint = metadata.get("end_session_endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint else None


def _maybe_bootstrap_admin(user: dict[str, Any], auth_settings: SettingsAuth) -> dict[str, Any]:
    """Auto-activate a first-login allowlisted user as ``system_admin``.

    Implements the first-admin bootstrap: when a freshly-upserted user is
    still ``pending`` **and** their email appears in
    ``auth_settings.bootstrap_admin_emails`` (an operator-configured
    allowlist, already lowercased/stripped by the ``SettingsAuth``
    validator), promote them to
    :data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` via
    :func:`ovid2.web.services.users_svc.activate` and record an explicit
    ``user.bootstrap_admin`` audit entry (on top of the ``user.activate``
    entry ``activate`` already writes).

    The guard on ``status == "pending"`` makes this operation safe and
    idempotent:

    * An **already-active** bootstrap admin re-login is a no-op (they are
      not ``pending``), so re-logging in never re-audits or re-activates.
    * A **disabled** user is never re-activated (``disabled != pending``).
    * A **non-allowlisted** email is never granted admin.

    Args:
        user: The user record dict returned by
            :func:`~ovid2.web.services.users_svc.upsert_from_idp` (must
            carry ``user_id``, ``email``, and ``status``).
        auth_settings: The active :class:`~ovid2.web.models.SettingsAuth`,
            whose ``bootstrap_admin_emails`` holds the allowlist.

    Returns:
        The (possibly updated) user record dict. When no promotion
        occurs, the input ``user`` is returned unchanged.
    """
    if user.get("status") != Role.PENDING.value:
        return user

    email_raw = user.get("email")
    if not isinstance(email_raw, str):
        return user
    email = email_raw.strip().lower()
    if not email or email not in auth_settings.bootstrap_admin_emails:
        return user

    user_id = str(user["user_id"])
    activated = users_svc.activate(user_id, Role.SYSTEM_ADMIN, actor="bootstrap")
    web_db.ensure_schema()
    web_db.audit(
        actor="bootstrap",
        action="user.bootstrap_admin",
        target=user_id,
        detail_json=f'{{"email": "{email}"}}',
    )
    logger.info("Bootstrapped first admin for allowlisted email.")
    return activated


# ---------------------------------------------------------------------------
# Login page + break-glass (todo 11)
# ---------------------------------------------------------------------------


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next: str | None = None) -> Response:
    """Render the login page, or redirect an already-authenticated user.

    Args:
        request: The inbound request.
        next: Optional post-login redirect target (validated).

    Returns:
        A redirect to the validated ``next`` (or ``/``) when the caller is
        already authenticated with a usable role, else the rendered login
        page (200) with provider buttons and — when configured — the
        break-glass form.
    """
    safe_next = _safe_next(next)
    if _is_authed(request):
        return RedirectResponse(url=safe_next, status_code=302)

    settings = settings_svc.get_settings()
    providers = oidc.list_enabled_providers(settings.auth)
    no_admin_configured = (
        settings.auth.enabled
        and not settings.auth.bootstrap_admin_emails
        and users_svc.count_active_admins() == 0
    )
    context: dict[str, Any] = {
        "request": request,
        "providers": providers,
        "breakglass_configured": breakglass.is_configured(),
        "next": safe_next,
        "error": None,
        "no_admin_configured": no_admin_configured,
    }
    return templates.TemplateResponse(request, "login.html", context)


@router.get("/login/{provider}", include_in_schema=False)
async def login_start(request: Request, provider: str, next: str | None = None) -> Response:
    """Start the OIDC authorization-code flow for ``provider``.

    Builds the Authlib authorize redirect, then lifts the ``state`` /
    ``nonce`` Authlib stashed in the request session into a signed
    transient cookie (alongside the validated ``next``) so the callback
    can validate them without server-side session middleware.

    Args:
        request: The inbound request.
        provider: The provider key to authenticate against.
        next: Optional post-login redirect target (validated).

    Returns:
        A 302 redirect to the provider's authorize endpoint, carrying the
        transient authorize-flow cookie. A 404 render of the login page
        when the provider is not enabled.
    """
    safe_next = _safe_next(next)
    settings = settings_svc.get_settings()
    enabled = {p.key for p in oidc.list_enabled_providers(settings.auth)}
    if provider not in enabled:
        logger.warning("Login attempted for unknown / disabled provider %r.", provider)
        return RedirectResponse(url="/login", status_code=302)

    registry = oidc.get_registry(settings.auth)
    client = registry.create_client(provider)

    # Give Authlib a dict-backed session to stash state/nonce into, so we do
    # not depend on SessionMiddleware. We lift the populated dict into our
    # own signed transient cookie immediately afterwards.
    request.scope.setdefault("session", {})

    redirect_uri = str(request.url_for("auth_callback", provider=provider))
    redirect: Response = await client.authorize_redirect(request, redirect_uri)

    session_data = dict(request.scope.get("session") or {})
    payload: dict[str, Any] = {
        "provider": provider,
        "next": safe_next,
        "session": session_data,
    }
    _stash_transient(redirect, request, payload)
    return redirect


@router.get("/auth/callback/{provider}", name="auth_callback", include_in_schema=False)
async def auth_callback(request: Request, provider: str) -> Response:
    """Finish the OIDC flow: validate, upsert the user, mint a session.

    Restores the ``state`` / ``nonce`` Authlib stashed at authorize time
    from the signed transient cookie, exchanges the code for tokens
    (Authlib ``authorize_access_token`` validates ``state`` + ``nonce``),
    upserts the user from the ID-token claims, creates a server-side
    session, and issues the signed session cookie.

    Args:
        request: The inbound callback request.
        provider: The provider key from the callback path.

    Returns:
        A 302 redirect to the validated ``next`` on success (carrying the
        session cookie and clearing the transient cookie), or a 400
        response when ``state`` / ``nonce`` cannot be validated (no
        session is minted).
    """
    transient = _read_transient(request)
    if transient is None or transient.get("provider") != provider:
        logger.warning("OIDC callback for %r with missing/mismatched transient state.", provider)
        return _reject_callback(request)

    # Restore Authlib's expected session bits so authorize_access_token can
    # validate the returned state against the one we issued.
    request.scope["session"] = dict(transient.get("session") or {})

    settings = settings_svc.get_settings()
    registry = oidc.get_registry(settings.auth)
    client = registry.create_client(provider)

    try:
        token = await client.authorize_access_token(request)
    except Exception:
        logger.warning("OIDC token exchange failed for provider %r.", provider, exc_info=True)
        return _reject_callback(request)

    if not isinstance(token, dict):
        logger.warning("OIDC token exchange returned a non-dict token for %r.", provider)
        return _reject_callback(request)

    claims = _claims_from_token(token)
    issuer_raw = claims.get("iss")
    issuer = issuer_raw if isinstance(issuer_raw, str) and issuer_raw else provider
    subject_raw = claims.get("sub")
    if not isinstance(subject_raw, str) or not subject_raw:
        logger.warning("OIDC token for %r carries no subject claim.", provider)
        return _reject_callback(request)
    email_raw = claims.get("email")
    email = email_raw if isinstance(email_raw, str) and email_raw else None
    name_raw = claims.get("name")
    name = name_raw if isinstance(name_raw, str) and name_raw else None

    user = users_svc.upsert_from_idp(issuer, subject_raw, email, name)
    user = _maybe_bootstrap_admin(user, settings.auth)

    idp_sid = _idp_sid_from_token(token)
    session = sessions_svc.create(
        user_id=str(user["user_id"]),
        idp_sid=idp_sid,
        absolute_ttl_min=settings.auth.absolute_ttl_minutes,
        idle_ttl_min=settings.auth.idle_ttl_minutes,
    )

    safe_next = _safe_next(
        transient.get("next") if isinstance(transient.get("next"), str) else None
    )
    response = RedirectResponse(url=safe_next, status_code=302)
    cookie.issue_cookie(response, session.session_id, request)
    _clear_transient(response)
    return response


def _reject_callback(request: Request) -> Response:
    """Return a 400 for a failed callback and clear the transient cookie.

    Args:
        request: The inbound callback request (unused beyond symmetry).

    Returns:
        A 400 response that mints no session and clears the transient
        authorize-flow cookie.
    """
    response = HTMLResponse(
        content="<h1>Login failed</h1><p>Invalid or expired login attempt. "
        '<a href="/login">Try again</a>.</p>',
        status_code=400,
    )
    _clear_transient(response)
    return response


@router.post("/login/breakglass", include_in_schema=False)
def login_breakglass(
    request: Request,
    password: str = Form(...),
    next: str | None = Form(default=None),
) -> Response:
    """Verify the break-glass password and mint a synthetic-admin session.

    Args:
        request: The inbound request.
        password: The candidate break-glass password (form field).
        next: Optional post-login redirect target (validated).

    Returns:
        On success: a 302 redirect to the validated ``next`` carrying the
        session cookie. On failure: a re-render of the login page (200)
        with an error message and no session.
    """
    safe_next = _safe_next(next)
    if breakglass.verify(password):
        settings = settings_svc.get_settings()
        session = sessions_svc.create(
            user_id=BREAKGLASS_USER_ID,
            idp_sid=None,
            absolute_ttl_min=settings.auth.absolute_ttl_minutes,
            idle_ttl_min=settings.auth.idle_ttl_minutes,
        )
        response = RedirectResponse(url=safe_next, status_code=302)
        cookie.issue_cookie(response, session.session_id, request)
        logger.info("Break-glass admin login succeeded.")
        return response

    logger.warning("Break-glass admin login failed.")
    settings = settings_svc.get_settings()
    providers = oidc.list_enabled_providers(settings.auth)
    context: dict[str, Any] = {
        "request": request,
        "providers": providers,
        "breakglass_configured": breakglass.is_configured(),
        "next": safe_next,
        "error": "Incorrect break-glass password.",
    }
    return templates.TemplateResponse(request, "login.html", context, status_code=401)


@router.get("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    """Revoke the current session, clear the cookie, and redirect out.

    Args:
        request: The inbound request carrying the session cookie.

    Returns:
        A 302 redirect to the IdP ``end_session_endpoint`` when one is
        discoverable for the session's provider, else to ``/login``. The
        session cookie is always cleared and the server-side session
        revoked (best-effort).
    """
    session_id = cookie.read_session_id(request)
    end_session: str | None = None
    if session_id is not None:
        session = sessions_svc.get_valid(session_id)
        sessions_svc.revoke(session_id)
        if session is not None and session.idp_sid:
            # Best-effort: prefer the first enabled provider's end-session
            # endpoint. Provider identity is not stored on the session, so
            # we consult the enabled providers in order.
            settings = settings_svc.get_settings()
            for provider in oidc.list_enabled_providers(settings.auth):
                end_session = await _end_session_endpoint(provider.key)
                if end_session:
                    break

    target = end_session or "/login"
    response = RedirectResponse(url=target, status_code=302)
    cookie.clear_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Silent recheck + front-channel logout (todo 12)
# ---------------------------------------------------------------------------


@router.get("/auth/recheck", include_in_schema=False)
def auth_recheck(request: Request) -> JSONResponse:
    """Return ``{"authenticated": bool}`` for the silent-recheck script.

    When ``settings.auth.silent_recheck`` is disabled, the endpoint always
    reports ``authenticated: true`` so the client script becomes a no-op
    (it never redirects). Otherwise it reports whether a valid, non-pending
    session resolves. Never raises — always returns JSON 200.

    Args:
        request: The inbound request.

    Returns:
        A JSON 200 response with the ``authenticated`` verdict.
    """
    try:
        settings = settings_svc.get_settings()
        if not settings.auth.silent_recheck:
            return JSONResponse({"authenticated": True})
        return JSONResponse({"authenticated": _is_authed(request)})
    except Exception:
        logger.debug("auth_recheck failed; failing open.", exc_info=True)
        return JSONResponse({"authenticated": True})


@router.get("/auth/frontchannel-logout", include_in_schema=False)
def frontchannel_logout(
    request: Request,
    sid: str | None = None,
    iss: str | None = None,
) -> Response:
    """Revoke sessions for an IdP-initiated front-channel logout.

    Microsoft (and other OIDC providers) call this registered URL in a
    hidden iframe when the user signs out at the IdP. It reads the ``sid``
    (IdP session id) and ``iss`` (issuer) query params and revokes every
    matching server-side session. Always returns 200 with
    ``Cache-Control: no-cache, no-store`` so intermediaries never cache
    the response.

    Args:
        request: The inbound request (unused beyond symmetry).
        sid: The IdP session id to revoke by (query param).
        iss: The issuer (query param, forwarded to the revoke call).

    Returns:
        A 200 response with no-store cache headers.
    """
    if sid:
        try:
            revoked = sessions_svc.revoke_by_idp_sid(iss or "", sid)
            logger.info("Front-channel logout revoked %d session(s).", revoked)
        except Exception:
            logger.warning("Front-channel logout failed to revoke sid.", exc_info=True)
    response = Response(status_code=200)
    response.headers["Cache-Control"] = "no-cache, no-store"
    return response


if __name__ == "__main__":
    raise SystemExit("auth is a library module; not runnable directly.")
