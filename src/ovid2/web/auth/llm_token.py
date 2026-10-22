"""Short-lived, GET-only bearer tokens for the assistant's tool loop.

When the right-panel assistant has tools enabled, the web tier mints one
token per chat turn and hands it to the ``python -m ovid2.llm chat``
executor child. The child uses it to call read-only ``/api/v1`` routes
*on behalf of the chatting user*:

- **Identity** — resolved by :func:`ovid2.web.auth.deps.current_user` to
  ``AuthContext(user_id="llm:<user>")`` so every request is attributable.
- **Role** — the chatting user's role, capped at
  :data:`~ovid2.web.auth.roles.Role.PIPELINE_OPERATOR` (never admin, even
  while auth is disabled and everyone is a synthetic admin).
- **Methods** — ``GET`` / ``HEAD`` only. Writes are proposals the human
  runs from the browser with their own session.
- **Lifetime** — expires after the turn timeout and is revoked as soon
  as the turn ends.

Tokens live only in this process's memory (sha256 digest → grant); the
plaintext never touches disk or logs. ``ovid2 web`` runs a single
uvicorn process, so the executor's callbacks land on the same table.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from ovid2.web.auth.roles import Role

__all__ = [
    "LLM_PRINCIPAL_PREFIX",
    "LlmGrant",
    "cap_role",
    "lookup",
    "mint",
    "revoke",
]

#: ``AuthContext.user_id`` prefix for requests made by the assistant.
LLM_PRINCIPAL_PREFIX = "llm:"

#: Token prefix — lets :func:`lookup` skip unrelated bearer tokens cheaply.
_TOKEN_PREFIX = "ovllm_"  # noqa: S105 - public prefix, not a secret

_RANK = {Role.PENDING: 0, Role.GUEST: 1, Role.VIEWER: 2,
         Role.PIPELINE_OPERATOR: 3, Role.SYSTEM_ADMIN: 4}

_LOCK = threading.Lock()
_GRANTS: dict[str, LlmGrant] = {}


@dataclass(frozen=True)
class LlmGrant:
    """What one assistant token is allowed to do.

    Attributes:
        user_id: The chatting user's id (``anonymous`` while auth is off).
        role: Effective (capped) role.
        email: The user's email when known.
        expires_at: ``time.monotonic()`` deadline.
    """

    user_id: str
    role: Role
    email: str | None
    expires_at: float


def cap_role(role: Role) -> Role:
    """Return ``role`` capped at ``pipeline_operator``."""
    if _RANK.get(role, 0) > _RANK[Role.PIPELINE_OPERATOR]:
        return Role.PIPELINE_OPERATOR
    return role


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge(now: float) -> None:
    for key in [k for k, g in _GRANTS.items() if g.expires_at <= now]:
        _GRANTS.pop(key, None)


def mint(user_id: str, role: Role, *, email: str | None, ttl_seconds: float) -> str:
    """Mint a token for one chat turn and return its plaintext.

    Args:
        user_id: The chatting user's id.
        role: The chatting user's role (capped here).
        email: Optional email for audit attribution.
        ttl_seconds: Lifetime; clamp to the turn timeout.
    """
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = time.monotonic()
    grant = LlmGrant(user_id=user_id, role=cap_role(role), email=email,
                     expires_at=now + max(1.0, float(ttl_seconds)))
    with _LOCK:
        _purge(now)
        _GRANTS[_digest(token)] = grant
    return token


def lookup(token: str) -> LlmGrant | None:
    """Return the live grant for ``token``, or ``None``."""
    if not token.startswith(_TOKEN_PREFIX):
        return None
    now = time.monotonic()
    with _LOCK:
        grant = _GRANTS.get(_digest(token))
        if grant is None:
            return None
        if grant.expires_at <= now:
            _GRANTS.pop(_digest(token), None)
            return None
        return grant


def revoke(token: str | None) -> None:
    """Revoke ``token`` (no-op when unknown or ``None``)."""
    if not token:
        return
    with _LOCK:
        _GRANTS.pop(_digest(token), None)


if __name__ == "__main__":
    raise SystemExit("llm_token is a library module; not runnable directly.")
