"""Service-principal bearer-token verifier for the MCP / agent tier.

The MCP server (``src/ovid2/mcp/``) is the untrusted planner in the
harness / API / executor trust model. It cannot hold a session cookie,
so when it needs to authenticate to the ovid2 API it presents a fixed
service-principal bearer token. This module maps a presented token onto
a :class:`~ovid2.web.auth.roles.Role`.

The token→role map is stored as a HASH map under
``$XDG_CONFIG_HOME/ovid2/secrets.d/auth_service_tokens.json`` (mode
``0o600``) with the shape ``{"<sha256hex>": "<role>"}``. Only the
SHA-256 hex digest of each token is persisted — the plaintext token is
never stored, logged, or returned.

Security invariants:

- The plaintext token is never logged or persisted; only its SHA-256
  digest is compared against the on-disk map.
- A token whose stored role string is missing or unparseable falls back
  to the configurable ``settings.auth.service_principal_role`` (default
  :data:`~ovid2.web.auth.roles.Role.PIPELINE_OPERATOR`) — never
  ``system_admin``.
- Lookup is by digest, so the presented plaintext never lands in an
  error message.
"""

from __future__ import annotations

import hashlib
import logging

from ovid2.config import secrets as _secrets
from ovid2.web.auth.roles import Role
from ovid2.web.services import settings_svc

logger = logging.getLogger(__name__)

__all__ = ["SECRET_NAME", "is_configured", "verify_token"]

#: Logical secret name (stem of the JSON blob under ``secrets.d/``).
SECRET_NAME: str = "auth_service_tokens"  # noqa: S105 — file stem, not a token

#: Hard floor for the fallback role. A service-principal token must never
#: silently escalate to ``system_admin`` even if the settings default is
#: mis-configured to that value.
_MAX_FALLBACK_ROLE: Role = Role.PIPELINE_OPERATOR


def _load_map() -> dict[str, str]:
    """Return the ``{sha256hex: role_string}`` token map, or an empty dict.

    Reads through :func:`ovid2.config.secrets.load_secret` (the same
    loader the rest of the app uses) so the ``0o600`` permission check
    applies. A missing secret file is treated as "no tokens configured".

    Returns:
        The token→role map with string keys and values; empty when no
        secret file exists or the payload is malformed.
    """
    try:
        payload = _secrets.load_secret(SECRET_NAME)
    except _secrets.SecretNotFoundError:
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _fallback_role() -> Role:
    """Return the configured fallback role, clamped away from admin.

    Reads ``settings.auth.service_principal_role`` and parses it onto a
    :class:`Role`. An unknown or admin value collapses to
    :data:`_MAX_FALLBACK_ROLE` so a service token can never resolve to
    ``system_admin`` via the fallback path.
    """
    configured = settings_svc.get_settings().auth.service_principal_role
    try:
        role = Role(configured)
    except ValueError:
        logger.warning(
            "Service-principal fallback role %r is not a known role; using pipeline_operator.",
            configured,
        )
        return _MAX_FALLBACK_ROLE
    if role == Role.SYSTEM_ADMIN:
        logger.warning(
            "Service-principal fallback role must not be system_admin; using pipeline_operator."
        )
        return _MAX_FALLBACK_ROLE
    return role


def is_configured() -> bool:
    """Return ``True`` iff at least one service token is configured on disk."""
    return bool(_load_map())


def verify_token(token: str) -> Role | None:
    """Resolve a presented bearer token to its mapped :class:`Role`.

    The token is SHA-256 hex-digested and looked up in the on-disk map.
    A hit whose stored role parses cleanly returns that role; a hit whose
    stored role is missing or unparseable returns the configured fallback
    (never ``system_admin``). A miss — or an empty token — returns
    ``None``.

    The plaintext token is never logged.

    Args:
        token: The candidate bearer token presented by the client.

    Returns:
        The mapped role, or ``None`` when the token is empty or unknown.
    """
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_map = _load_map()
    stored_role = token_map.get(digest)
    if stored_role is None:
        return None
    try:
        role = Role(stored_role)
    except ValueError:
        logger.warning("Service token carries unknown role string; using fallback.")
        return _fallback_role()
    if role == Role.SYSTEM_ADMIN:
        logger.warning("Service token mapped to system_admin; downgrading to fallback.")
        return _fallback_role()
    return role


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("service_token is a library module; not runnable directly.")
