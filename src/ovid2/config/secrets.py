"""Secrets and credential loading.

Reads credentials from XDG-compliant paths in this order:

0. ``$OVID2_SECRETS_DIR/<name>.json`` — the executor's **scoped**
   directory: a private per-invocation copy of just the secrets one run
   was granted (see :mod:`ovid2.config.executor_env`). With
   ``OVID2_SECRETS_SCOPE_MODE=enforce`` it is the *only* place searched;
   under the default ``warn`` a miss falls back to the paths below and
   logs a warning naming the under-declared secret.
1. ``$OVID2_SECRETS_FILE`` (explicit override — full path to a JSON blob)
2. ``$XDG_CONFIG_HOME/ovid2/secrets.d/<name>.json``
3. ``~/.config/ovid2/secrets.d/<name>.json``
4. ``/etc/ovid2/secrets.d/<name>.json`` (system install)

Every secret file MUST be mode ``0600`` and owned by the running user. The
loader refuses to open files that are world-readable to reduce the chance
of accidental leakage.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

__all__ = [
    "SCOPED_SECRETS_DIR_ENV",
    "SCOPE_MODE_ENV",
    "SecretNotFoundError",
    "SecretPermissionError",
    "load_secret",
    "scope_enforced",
    "scoped_file",
    "scoped_secrets_dir",
]


logger = logging.getLogger(__name__)


class SecretNotFoundError(FileNotFoundError):
    """Raised when no secret file with the requested name can be found."""


class SecretPermissionError(PermissionError):
    """Raised when a secret file exists but is world-readable."""


#: Environment variable naming an exclusive, scoped secrets directory.
SCOPED_SECRETS_DIR_ENV: str = "OVID2_SECRETS_DIR"

#: ``warn`` (default during roll-out) or ``enforce``. Under ``warn`` a
#: secret missing from :data:`SCOPED_SECRETS_DIR_ENV` is still resolved
#: from the unscoped search path, with a warning naming it, so operators
#: can find under-declared scopes before switching to ``enforce``.
SCOPE_MODE_ENV: str = "OVID2_SECRETS_SCOPE_MODE"


def scoped_secrets_dir() -> Path | None:
    """Return the active scoped secrets directory, or ``None``."""
    scoped = os.environ.get(SCOPED_SECRETS_DIR_ENV)
    return Path(scoped) if scoped else None


def scope_enforced() -> bool:
    """Return ``True`` when a scope miss must fail instead of falling back."""
    return os.environ.get(SCOPE_MODE_ENV, "warn").strip().lower() == "enforce"


def scoped_file(file_name: str) -> Path | None:
    """Resolve ``file_name`` against the active scope.

    Shared by every reader of ``secrets.d`` that is not :func:`load_secret`
    (per-pipeline salts, the XOR seed file) so the scope applies to them
    too. Those files are *optional*, so an enforced miss is not an error
    here: the caller receives the (non-existent) scoped path and applies
    its own "absent" behaviour.

    Returns:
        - ``None`` when no scope is active, or when the file is out of
          scope under ``warn`` mode (the caller then uses its normal
          unscoped search path; a warning is logged).
        - The scoped path otherwise — existing, or (under ``enforce``)
          possibly absent, in which case the caller must not fall back.
    """
    scoped = scoped_secrets_dir()
    if scoped is None:
        return None
    path = scoped / file_name
    if path.is_file() or scope_enforced():
        return path
    logger.warning(
        "Secret file %r is outside this executor's declared scope; falling back "
        "to the unscoped search path. This will fail once %s=enforce.",
        file_name,
        SCOPE_MODE_ENV,
    )
    return None


def _secret_candidates(name: str) -> list[Path]:
    """Return the ordered list of candidate paths for a named secret."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise ValueError(f"Invalid secret name {name!r}.")

    scoped = scoped_secrets_dir()
    if scoped is not None and scope_enforced():
        return [scoped / f"{name}.json"]

    candidates: list[Path] = []
    if scoped is not None:
        candidates.append(scoped / f"{name}.json")

    override = os.environ.get("OVID2_SECRETS_FILE")
    if override:
        candidates.append(Path(override))

    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates.append(Path(xdg_config) / "ovid2" / "secrets.d" / f"{name}.json")
    candidates.append(Path("/etc/ovid2/secrets.d") / f"{name}.json")
    return candidates


def _check_permissions(path: Path) -> None:
    """Refuse to load a secret file that is world-readable.

    Args:
        path: The secret file path.

    Raises:
        SecretPermissionError: If any world-readable bit is set.
    """
    mode = path.stat().st_mode
    if mode & (stat.S_IROTH | stat.S_IWOTH):
        raise SecretPermissionError(
            f"Secret file {path} is world-readable ({oct(mode & 0o777)}). "
            f"Run `chmod 600 {path}` and retry."
        )


def load_secret(name: str) -> dict[str, Any]:
    """Load a JSON secret by logical name.

    Args:
        name: Logical secret name (e.g. ``"snowflake"``, ``"datavant"``).
            Resolved to ``<candidate>/secrets.d/<name>.json``.

    Returns:
        The parsed JSON contents.

    Raises:
        SecretNotFoundError: If no candidate path exists.
        SecretPermissionError: If the file is world-readable.
    """
    candidates = _secret_candidates(name)
    scoped = scoped_secrets_dir()
    for candidate in candidates:
        if candidate.is_file():
            if scoped is not None and candidate.parent != scoped:
                logger.warning(
                    "Secret %r is outside this executor's declared scope; resolved "
                    "from %s instead. This will fail once %s=enforce.",
                    name,
                    candidate,
                    SCOPE_MODE_ENV,
                )
            _check_permissions(candidate)
            with candidate.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Secret file {candidate} must contain a JSON object, "
                    f"got {type(data).__name__}."
                )
            return data

    tried = ", ".join(str(c) for c in candidates)
    if scoped is not None and scope_enforced():
        raise SecretNotFoundError(
            f"Secret '{name}' is not in this executor's scoped secrets "
            f"directory ({tried}). The run was not granted it — check that "
            "the connection / address / engine using it is named in the "
            "request (see docs/installer.md, 'Scoped executor secrets')."
        )
    raise SecretNotFoundError(
        f"Secret '{name}' not found in any of: {tried}. "
        f"See docs/installer.md for the secrets layout."
    )
