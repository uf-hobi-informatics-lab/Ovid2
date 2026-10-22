"""FastAPI dependency layer for request-time authentication + authorization.

This module is the single seam every HTML / REST route uses to gate access
(the wiring into routes lands in later todos — this module only *defines* the
dependencies and their behavior). It resolves the current user from the signed
session cookie and enforces role / capability checks.

Backwards-compatibility guarantee
----------------------------------
When ``settings.auth.enabled`` is ``False`` the whole layer is a no-op: every
resolution yields a synthetic :class:`AuthContext` for a
:data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` (``anonymous_bypass=True``),
so every ``require_*`` dependency passes and the panel behaves exactly as it
does today (fully open access). No check may block while auth is disabled.

Resolution model (auth enabled)
--------------------------------
:func:`current_user` reads the session id from the request cookie
(:func:`ovid2.web.auth.cookie.read_session_id`), validates it against the
session store (:func:`ovid2.web.services.sessions_svc.get_valid`), loads the
user record (:func:`ovid2.web.services.users_svc.get_user`), slides the idle
window (:func:`ovid2.web.services.sessions_svc.touch`), and maps the stored
role string onto :class:`~ovid2.web.auth.roles.Role`.

``None`` is returned **only** when there is genuinely no trustworthy session
(no cookie, an expired / revoked / tampered cookie, or a session whose user
row has been deleted). A ``pending`` or ``disabled`` user *does* have a valid
session and a user row, so :func:`current_user` still returns an
:class:`AuthContext` for them — mapped onto their stored role
(``pending`` → :data:`~ovid2.web.auth.roles.Role.PENDING`, whose capability
set is empty) so the ``require_*`` dependencies naturally answer ``403``
(authenticated but not authorized) rather than ``401`` (unauthenticated).

``AuthContext`` is deliberately shaped so the later break-glass login (todo 11
— synthetic ``system_admin`` with ``user_id="breakglass"``) and the MCP
service-principal (todo 21 — ``user_id="mcp:<name>"`` with a mapped role) can
be represented without a rewrite. Those flows are **not** implemented here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from ovid2.web.auth.roles import Capability, Role, has_capability
from ovid2.web.services import sessions_svc, settings_svc, users_svc

logger = logging.getLogger(__name__)

__all__ = [
    "ANONYMOUS_USER_ID",
    "AuthContext",
    "current_user",
    "require_capability",
    "require_role",
]

#: ``user_id`` stamped on the synthetic admin returned while auth is disabled.
ANONYMOUS_USER_ID: str = "anonymous"

#: The user status that grants access. Any other status (``pending`` /
#: ``disabled``) still resolves to an :class:`AuthContext`, but the role it
#: carries determines authorization (``pending`` → no capabilities).
_ACTIVE_STATUS: str = "active"


@dataclass(frozen=True)
class AuthContext:
    """The authenticated principal resolved for the current request.

    Attributes:
        user_id: Stable identifier for the principal. For a real user this is
            the ``web_users.user_id``; for the disabled-auth bypass it is
            :data:`ANONYMOUS_USER_ID`. Later flows reuse this field for the
            break-glass admin (``"breakglass"``) and MCP service principals
            (``"mcp:<name>"``); those are not implemented in this module.
        role: The principal's :class:`~ovid2.web.auth.roles.Role`, which drives
            every capability decision.
        email: The principal's email address when known, else ``None``.
        anonymous_bypass: ``True`` only for the synthetic admin returned while
            ``settings.auth.enabled`` is ``False``. Real principals always have
            this ``False``.
    """

    user_id: str
    role: Role
    email: str | None = None
    anonymous_bypass: bool = False


def _synthetic_admin() -> AuthContext:
    """Return the open-access admin used while authentication is disabled."""
    return AuthContext(
        user_id=ANONYMOUS_USER_ID,
        role=Role.SYSTEM_ADMIN,
        email=None,
        anonymous_bypass=True,
    )


def _role_from_user(record: dict[str, object]) -> Role:
    """Map a ``web_users`` record's ``role`` string onto :class:`Role`.

    An unrecognised or missing role string is treated as
    :data:`~ovid2.web.auth.roles.Role.PENDING` (the empty-capability role), so
    a corrupt row can never accidentally grant access.

    Args:
        record: A user record dict as returned by
            :func:`ovid2.web.services.users_svc.get_user`.

    Returns:
        The resolved role.
    """
    raw = record.get("role")
    if isinstance(raw, str):
        try:
            return Role(raw)
        except ValueError:
            logger.warning(
                "User %r carries unknown role %r; treating as PENDING.",
                record.get("user_id"),
                raw,
            )
    return Role.PENDING


def current_user(request: Request) -> AuthContext | None:
    """Resolve the authenticated principal for ``request``.

    Args:
        request: The inbound request carrying (or lacking) the session cookie.

    Returns:
        - While ``settings.auth.enabled`` is ``False``: the synthetic
          :data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` bypass context.
        - While auth is enabled and a valid session resolves to an existing
          user row: an :class:`AuthContext` for that user (including
          ``pending`` / ``disabled`` users — their role, not their status,
          governs authorization).
        - ``None`` when there is no trustworthy session: no cookie, an
          expired / revoked / tampered cookie, or a session whose user row no
          longer exists.
    """
    # The assistant's per-turn token is honoured (and its GET-only /
    # capped-role limits enforced) even while auth is disabled — it must
    # never inherit the synthetic-admin bypass.
    llm_ctx = _llm_principal(request)
    if llm_ctx is not None:
        return llm_ctx

    settings = settings_svc.get_settings()
    if not settings.auth.enabled:
        return _synthetic_admin()

    session_id = cookie_read_session_id(request)
    if session_id is None:
        return _service_principal(request)

    session = sessions_svc.get_valid(session_id)
    if session is None:
        return _service_principal(request)

    record = users_svc.get_user(session.user_id)
    if record is None:
        # A valid session pointing at a deleted user is not trustworthy.
        return _service_principal(request)

    # Slide the idle window now that we have a live, resolvable session.
    sessions_svc.touch(session_id, settings.auth.idle_ttl_minutes)

    email_raw = record.get("email")
    email = email_raw if isinstance(email_raw, str) else None
    return AuthContext(
        user_id=str(record["user_id"]),
        role=_role_from_user(record),
        email=email,
        anonymous_bypass=False,
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def _llm_principal(request: Request) -> AuthContext | None:
    """Resolve an assistant tool-loop token (see :mod:`ovid2.web.auth.llm_token`).

    Returns ``None`` when the request carries no assistant token. When it
    does, the token is only valid for ``GET`` / ``HEAD`` on ``/api/v1``;
    anything else is rejected with ``403`` (an expired / unknown
    assistant token → ``401``) so a model can never mutate state.

    Raises:
        HTTPException: 401 / 403 as described above.
    """
    from ovid2.web.auth import llm_token

    credential = _bearer(request)
    if credential is None or not credential.startswith("ovllm_"):
        return None
    grant = llm_token.lookup(credential)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Assistant token expired or unknown.")
    path = request.url.path
    if request.method.upper() not in ("GET", "HEAD") or not path.startswith("/api/v1/"):
        logger.warning("Rejected assistant %s %s (read-only token).", request.method, path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="The assistant token is read-only.")
    return AuthContext(
        user_id=f"{llm_token.LLM_PRINCIPAL_PREFIX}{grant.user_id}",
        role=grant.role,
        email=grant.email,
        anonymous_bypass=False,
    )


def _service_principal(request: Request) -> AuthContext | None:
    """Resolve a service-principal bearer token to an :class:`AuthContext`.

    Consulted only after the session-cookie path has failed to produce a
    trustworthy session (the cookie always takes precedence). Reads an
    ``Authorization: Bearer <token>`` header and maps it via
    :func:`ovid2.web.auth.service_token.verify_token`. A missing header,
    a non-bearer scheme, or an unknown token yields ``None`` (→ 401 on a
    gated endpoint). The token is never logged. Imported lazily to avoid
    a module-import cycle (mirrors :func:`cookie_read_session_id`).

    Args:
        request: The inbound request that lacked a valid session cookie.

    Returns:
        An :class:`AuthContext` for the mapped role, or ``None``.
    """
    from ovid2.web.auth import service_token

    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    role = service_token.verify_token(credential.strip())
    if role is None:
        return None
    return AuthContext(
        user_id="mcp:service",
        role=role,
        email=None,
        anonymous_bypass=False,
    )


def cookie_read_session_id(request: Request) -> str | None:
    """Return the verified session id from ``request`` (or ``None``).

    Thin indirection over :func:`ovid2.web.auth.cookie.read_session_id` so the
    dependency layer keeps a single import surface. Imported lazily to avoid a
    module-import cycle between the cookie manager and this module.

    Args:
        request: The inbound request carrying the cookie.

    Returns:
        The trusted session id, or ``None`` when absent / untrusted.
    """
    from ovid2.web.auth import cookie

    return cookie.read_session_id(request)


def require_capability(cap: Capability) -> Callable[[Request], AuthContext]:
    """Build a FastAPI dependency that requires capability ``cap``.

    Behavior when ``settings.auth.enabled`` is ``True``:

    - **Unauthenticated** (no valid session / no user row) →
      ``HTTPException(401)``.
    - **Authenticated but unauthorized** — the resolved role lacks ``cap``
      (this includes ``pending`` / ``disabled`` users, which map to
      :data:`~ovid2.web.auth.roles.Role.PENDING` with no capabilities) →
      ``HTTPException(403)``.

    When auth is disabled the synthetic
    :data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` bypass passes everything.

    Args:
        cap: The capability the caller's role must hold.

    Returns:
        A dependency callable returning the resolved :class:`AuthContext` on
        success and raising :class:`fastapi.HTTPException` otherwise.
    """

    def _dependency(request: Request) -> AuthContext:
        ctx = current_user(request)
        if ctx is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if not has_capability(ctx.role, cap):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return ctx

    return _dependency


def require_role(role: Role) -> Callable[[Request], AuthContext]:
    """Build a FastAPI dependency that requires a specific ``role``.

    Authorization rule (kept deliberately simple, as the plan permits): the
    caller passes when their resolved role equals ``role`` **or** when they are
    a :data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` (the admin is a superuser
    and satisfies every role gate). Every other role → ``403``.

    Behavior when ``settings.auth.enabled`` is ``True``:

    - **Unauthenticated** → ``HTTPException(401)``.
    - **Authenticated but wrong role** (and not ``system_admin``) →
      ``HTTPException(403)``.

    When auth is disabled the synthetic admin bypass passes everything.

    Args:
        role: The role the caller must hold (``system_admin`` always passes).

    Returns:
        A dependency callable returning the resolved :class:`AuthContext` on
        success and raising :class:`fastapi.HTTPException` otherwise.
    """

    def _dependency(request: Request) -> AuthContext:
        ctx = current_user(request)
        if ctx is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if ctx.role != role and ctx.role != Role.SYSTEM_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return ctx

    return _dependency


if __name__ == "__main__":
    raise SystemExit("deps is a library module; not runnable directly.")
