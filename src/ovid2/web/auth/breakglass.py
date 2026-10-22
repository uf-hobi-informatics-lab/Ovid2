"""Break-glass local admin verifier.

Ovid2 delegates all *ordinary* authentication to an external OIDC
identity provider (Google / Microsoft). The single exception is a
sealed **break-glass** local admin — one password, hashed with argon2,
stored under ``$XDG_CONFIG_HOME/ovid2/secrets.d/auth_breakglass.json``
(mode ``0o600``). It exists so an operator can never be permanently
locked out of the web panel: a successful break-glass login maps to a
synthetic ``system_admin`` identity (that wiring lives in the login
routes / auth dependencies, not here).

This module owns exactly three concerns:

1. **Set** the password (``set_password``) — hash with argon2 and
   persist through :mod:`ovid2.web.services.secrets_svc` so the strict
   ``0o600`` write path and the ``web_audit`` trail both apply.
2. **Probe** whether a password has been configured
   (``is_configured``) by reading the hash back through
   :func:`ovid2.config.secrets.load_secret`.
3. **Verify** a candidate password (``verify``) in constant time via
   argon2, protected by an in-memory consecutive-failure lockout.

Security invariants:

- The plaintext password is never stored, logged, or returned.
- The argon2 hash is never returned by any public function — only a
  ``bool`` verdict escapes.
- Verification is constant-time (argon2's ``PasswordHasher.verify``).
- After :data:`MAX_FAILURES` consecutive wrong attempts the verifier
  locks for :data:`LOCKOUT_SECONDS`; while locked ``verify`` returns
  ``False`` immediately without touching the hash. A correct password
  (checked only when not locked) resets the failure counter.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import argon2
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ovid2.config import secrets as _secrets
from ovid2.web import db as web_db
from ovid2.web.models import SecretWrite
from ovid2.web.services import secrets_svc

logger = logging.getLogger(__name__)

__all__ = [
    "LOCKOUT_SECONDS",
    "MAX_FAILURES",
    "SECRET_KEY",
    "SECRET_NAME",
    "is_configured",
    "set_password",
    "verify",
]

#: Logical secret name (stem of the JSON blob under ``secrets.d/``).
SECRET_NAME: str = "auth_breakglass"  # noqa: S105 — file stem, not a password

#: JSON key holding the argon2 hash inside the secret file.
SECRET_KEY: str = "password_hash"  # noqa: S105 — JSON key name, not a secret value

#: Consecutive failed attempts before the verifier locks out.
MAX_FAILURES: int = 5

#: Lockout window, in seconds, applied once :data:`MAX_FAILURES` is hit.
LOCKOUT_SECONDS: int = 300


@dataclass
class _LockoutState:
    """In-memory failed-attempt tracking for the break-glass verifier.

    Attributes:
        failures: Count of consecutive failed verifications since the
            last success (or reset).
        locked_until: Monotonic clock deadline (from :func:`time.monotonic`)
            before which every verification short-circuits to ``False``.
            ``0.0`` means "not locked".
    """

    failures: int = 0
    locked_until: float = 0.0


#: Module-level lockout state guarded by :data:`_LOCK`.
_STATE = _LockoutState()

#: Protects :data:`_STATE` against concurrent request threads.
_LOCK = threading.Lock()

#: Shared argon2 hasher — parameters are the argon2-cffi defaults.
_HASHER = PasswordHasher()


def _reset_lockout() -> None:
    """Reset the in-memory lockout state (test hook).

    Clears the failure counter and any active lock so a subsequent
    :func:`verify` starts from a clean slate. Intended for unit tests;
    production code never needs to call it.
    """
    with _LOCK:
        _STATE.failures = 0
        _STATE.locked_until = 0.0


def _load_hash() -> str | None:
    """Return the stored argon2 hash, or ``None`` if unset.

    Reads through :func:`ovid2.config.secrets.load_secret` (the same
    loader the rest of the app uses) so the ``0o600`` permission check
    applies. The hash never leaves this module.

    Returns:
        The argon2 hash string, or ``None`` when no break-glass password
        has been configured.
    """
    try:
        payload = _secrets.load_secret(SECRET_NAME)
    except _secrets.SecretNotFoundError:
        return None
    value = payload.get(SECRET_KEY)
    if not isinstance(value, str) or not value:
        return None
    return value


def is_configured() -> bool:
    """Return ``True`` iff a break-glass password hash exists on disk.

    Returns:
        ``True`` when :data:`SECRET_NAME` holds a non-empty
        :data:`SECRET_KEY` argon2 hash; ``False`` otherwise.
    """
    return _load_hash() is not None


def set_password(password: str, actor: str) -> None:
    """Hash ``password`` with argon2 and persist it as the break-glass secret.

    The hash is written through
    :func:`ovid2.web.services.secrets_svc.write_secret`, which enforces
    the ``0o600`` file mode and appends a ``secret.*`` row to
    ``web_audit``. This function additionally audits a dedicated
    ``breakglass.set`` action so the security-relevant event is
    unambiguous. The plaintext ``password`` is never stored or logged.

    Setting a new password also clears any active in-memory lockout so
    an operator who just reset the credential is not blocked by a stale
    failure counter.

    Args:
        password: The new break-glass password (plaintext, never
            persisted).
        actor: Identifier of who set the password, recorded in the
            audit trail (e.g. ``cli`` or ``web``).

    Raises:
        ValueError: If ``password`` is empty.
        ovid2.web.services.secrets_svc.SecretError: If the secret file
            cannot be written safely (e.g. a pre-existing file with
            permissive bits).
    """
    if not password:
        raise ValueError("Break-glass password must not be empty.")
    hashed = _HASHER.hash(password)
    secrets_svc.write_secret(
        SECRET_NAME,
        SecretWrite(values={SECRET_KEY: hashed}),
        actor=actor,
    )
    web_db.ensure_schema()
    web_db.audit(actor=actor, action="breakglass.set", target=SECRET_NAME)
    _reset_lockout()
    logger.info("Break-glass admin password set by %s", actor)


def verify(password: str) -> bool:
    """Verify ``password`` against the stored break-glass hash.

    Behaviour:

    - Returns ``False`` immediately when no password is configured.
    - Returns ``False`` immediately (without checking the hash) while a
      lockout window is active.
    - On a mismatch, increments the consecutive-failure counter and
      triggers a :data:`LOCKOUT_SECONDS` lockout once the counter
      reaches :data:`MAX_FAILURES`.
    - On a match, resets the failure counter and returns ``True``.

    The comparison is constant-time (argon2's ``verify``). The plaintext
    password and the stored hash are never logged.

    Args:
        password: The candidate password to check.

    Returns:
        ``True`` if the password matches and the verifier is not locked
        out; ``False`` otherwise.
    """
    stored = _load_hash()
    if stored is None:
        return False

    now = time.monotonic()
    with _LOCK:
        if _STATE.locked_until > now:
            logger.warning(
                "Break-glass verify rejected: locked out for %.0fs more.",
                _STATE.locked_until - now,
            )
            return False

    try:
        _HASHER.verify(stored, password)
    except VerifyMismatchError:
        with _LOCK:
            _STATE.failures += 1
            if _STATE.failures >= MAX_FAILURES:
                _STATE.locked_until = time.monotonic() + LOCKOUT_SECONDS
                logger.warning(
                    "Break-glass locked out after %d consecutive failures.",
                    _STATE.failures,
                )
        return False
    except argon2.exceptions.InvalidHashError:
        # A malformed / non-argon2 hash on disk is a configuration
        # problem, not a caller mistake. Refuse the login rather than
        # crashing the request handler.
        logger.error("Break-glass hash on disk is not a valid argon2 hash.")
        return False

    with _LOCK:
        _STATE.failures = 0
        _STATE.locked_until = 0.0
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("breakglass is a library module; not runnable directly.")
