"""Secrets and credential loading.

Reads credentials from XDG-compliant paths in this order:

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
import os
import stat
from pathlib import Path
from typing import Any

__all__ = ["SecretNotFoundError", "SecretPermissionError", "load_secret"]


class SecretNotFoundError(FileNotFoundError):
    """Raised when no secret file with the requested name can be found."""


class SecretPermissionError(PermissionError):
    """Raised when a secret file exists but is world-readable."""


def _secret_candidates(name: str) -> list[Path]:
    """Return the ordered list of candidate paths for a named secret."""
    candidates: list[Path] = []

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
    for candidate in _secret_candidates(name):
        if candidate.is_file():
            _check_permissions(candidate)
            with candidate.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Secret file {candidate} must contain a JSON object, "
                    f"got {type(data).__name__}."
                )
            return data

    tried = ", ".join(str(c) for c in _secret_candidates(name))
    raise SecretNotFoundError(
        f"Secret '{name}' not found in any of: {tried}. "
        f"See docs/installer.md for the secrets layout."
    )
