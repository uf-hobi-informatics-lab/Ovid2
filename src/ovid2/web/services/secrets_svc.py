"""Secret editor service (W4).

Owns the write path for JSON secret files under
``$XDG_CONFIG_HOME/ovid2/secrets.d/``. The read path never returns
values — only key names — so the UI can safely display the shape of a
secret without giving an operator a way to exfiltrate credentials via
the browser.

Safety rules enforced here:

1. Every write uses :func:`os.open` with ``mode=0o600`` and
   ``O_CREAT | O_WRONLY | O_TRUNC`` so the file is never briefly
   world-readable during the write.
2. Overwrites of existing files that are *not already* ``0o600`` are
   refused; the operator must ``chmod 600`` the file first or delete
   it, mirroring the loader in :mod:`ovid2.config.secrets`.
3. The service never returns secret *values* through any code path
   (``keys()`` only). A value can only be replaced with the JSON body
   the operator sends on ``PUT``.
4. Existing values are preserved when the client sends the sentinel
   string ``"***"`` for a key.

The contract test in ``tests/contract/test_web_secret_permissions.py``
requires that any module writing under ``secrets.d/`` also mentions
``0o600`` in its source — hence the explicit constant references
below.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.config import paths as _paths
from ovid2.web import db as web_db
from ovid2.web.models import SecretDetail, SecretSummary, SecretWrite, WriteResult

logger = logging.getLogger(__name__)

__all__ = [
    "SECRET_FILE_MODE",
    "SENTINEL_MASK",
    "SecretError",
    "delete_secret",
    "get_secret",
    "list_secrets",
    "write_secret",
]

#: Required permission bits for every file under ``secrets.d/`` — owner rw only.
SECRET_FILE_MODE: int = 0o600

#: Sentinel echoed back for masked values on ``PUT`` (see
#: :func:`_merge_masked_updates`).
SENTINEL_MASK: str = "***"


class SecretError(ValueError):
    """Raised when a secret write is invalid or the file is unsafe to touch."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _secrets_dir() -> Path:
    """Return the user-scoped secrets directory, creating it on demand.

    The parent :func:`ovid2.config.paths.config_dir` is ``0o755`` — the
    ``secrets.d`` subdirectory itself is created with ``0o700`` so a
    misconfigured ``umask`` cannot accidentally leak file listings.
    """
    directory = _paths.secrets_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Best-effort tighten in case the directory already existed with
    # looser bits — no-op when we do not own it.
    try:
        current_mode = stat.S_IMODE(directory.stat().st_mode)
        if current_mode & (stat.S_IRWXG | stat.S_IRWXO):
            os.chmod(directory, 0o700)
    except PermissionError:
        pass
    return directory


def _target_path(name: str) -> Path:
    """Return the write target for a secret name (user-scoped)."""
    _validate_name(name)
    return _secrets_dir() / f"{name}.json"


def _existing_path(name: str) -> Path | None:
    """Return the on-disk path of a secret file, or ``None``.

    Searches the same order as :func:`ovid2.config.secrets.load_secret`
    minus the ``OVID2_SECRETS_FILE`` override (that variable points at a
    single explicit file and is out of scope for the editor).
    """
    _validate_name(name)
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates = [
        Path(xdg_config) / "ovid2" / "secrets.d" / f"{name}.json",
        Path("/etc/ovid2/secrets.d") / f"{name}.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _validate_name(name: str) -> None:
    """Reject filename stems that could escape ``secrets.d/``."""
    if not name:
        raise SecretError("Secret name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise SecretError(f"Invalid secret name '{name}'. Use letters, digits, '-', '_', '.' only.")
    if name.startswith("."):
        raise SecretError(f"Secret name '{name}' is reserved.")


def _mode_octal(path: Path) -> str:
    """Return the mode of ``path`` as a four-digit octal string, e.g. ``0600``."""
    return f"0{stat.S_IMODE(path.stat().st_mode):o}"


def _iso_mtime(path: Path) -> str:
    """Return the mtime of ``path`` as an ISO-8601 UTC timestamp."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(microsecond=0).isoformat()


def _ensure_safe_to_write(path: Path) -> None:
    """Refuse to overwrite an existing file that is not already 0o600.

    Rationale: the loader
    (:func:`ovid2.config.secrets.load_secret`) already refuses to open
    files with permissive bits. If we were willing to silently
    ``chmod 600`` before overwriting, we could paper over a real
    permissions incident. Instead we surface the problem so the
    operator can decide.
    """
    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != SECRET_FILE_MODE:
        raise SecretError(
            f"Refusing to overwrite {path} with mode {mode:04o}. "
            f"Run `chmod 600 {path}` (or delete it) and retry."
        )


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _read_raw(path: Path) -> dict[str, Any]:
    """Return the on-disk contents of a secret file (never returned to the API)."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SecretError(f"Secret file {path} is not a JSON object.")
    return data


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def list_secrets() -> list[SecretSummary]:
    """Return every secret file found under the user-scoped ``secrets.d/``.

    Values are *never* returned; only key names, permission bits, and
    metadata that would already be visible to anyone with shell access.
    Files with unsafe modes are still listed — the UI colours them so
    the operator can spot the mismatch.
    """
    directory = _secrets_dir()
    summaries: list[SecretSummary] = []
    if not directory.is_dir():
        return summaries
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            payload = _read_raw(entry)
        except (OSError, json.JSONDecodeError, SecretError):
            continue
        summaries.append(
            SecretSummary(
                name=entry.stem,
                path=str(entry),
                mode=_mode_octal(entry),
                size_bytes=entry.stat().st_size,
                mtime=_iso_mtime(entry),
                keys=sorted(payload.keys()),
            )
        )
    return summaries


def get_secret(name: str) -> SecretDetail | None:
    """Return the shape (keys only) of one secret file, or ``None`` if missing."""
    path = _existing_path(name)
    if path is None:
        return None
    try:
        payload = _read_raw(path)
    except (OSError, json.JSONDecodeError, SecretError):
        return None
    return SecretDetail(
        name=name,
        path=str(path),
        mode=_mode_octal(path),
        keys=sorted(payload.keys()),
    )


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def _merge_masked_updates(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Preserve existing values for keys sent as the mask sentinel."""
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if value == SENTINEL_MASK:
            existing_value = (existing or {}).get(key)
            if existing_value in (None, ""):
                raise SecretError(
                    f"Field '{key}' is masked but has no value on disk. "
                    "Enter the real value before saving."
                )
            merged[key] = existing_value
        else:
            merged[key] = value
    return merged


def write_secret(
    name: str,
    payload: SecretWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Create or update a secret JSON file with strict ``0o600`` semantics.

    The file is opened via :func:`os.open` with ``mode=0o600`` from the
    start; ``os.chmod(path, 0o600)`` runs afterwards as a belt-and-
    braces guarantee in case the ``umask`` masks the initial mode.

    Args:
        name: Filename stem — validated via :func:`_validate_name`.
        payload: Parsed :class:`SecretWrite` from the client.
        actor: Free-form origin label recorded in ``web_audit``.
        create_only: When True, refuse to overwrite an existing file.

    Returns:
        A :class:`WriteResult` describing the write.

    Raises:
        SecretError: On validation failure, permissive-mode conflict,
            or a create-only conflict.
    """
    _validate_name(name)
    if not isinstance(payload.values, dict) or not payload.values:
        raise SecretError("Secret payload must contain at least one key.")

    for key, value in payload.values.items():
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise SecretError(
                f"Value for key '{key}' must be a JSON scalar (str/number/bool/null)."
            )

    target = _target_path(name)
    if target.exists() and create_only:
        raise SecretError(f"Secret '{name}' already exists.")
    _ensure_safe_to_write(target)

    existing_raw: dict[str, Any] | None = None
    if target.exists():
        try:
            existing_raw = _read_raw(target)
        except (OSError, json.JSONDecodeError, SecretError) as exc:
            raise SecretError(f"Existing secret is not readable: {exc}") from exc

    merged = _merge_masked_updates(existing_raw, dict(payload.values))
    serialised = json.dumps(merged, indent=2, sort_keys=True) + "\n"

    # os.open with mode=0o600 guarantees the file never exists briefly
    # with the process umask. See tests/contract/test_web_secret_permissions.py.
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        SECRET_FILE_MODE,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialised)
    except BaseException:
        # Any failure mid-write leaves an empty (or truncated) file on
        # disk — remove it so the operator does not think the write
        # succeeded.
        target.unlink(missing_ok=True)
        raise
    # Belt-and-braces: force 0o600 even if the umask masked the
    # os.open() mode argument on a filesystem that ignores it.
    os.chmod(target, SECRET_FILE_MODE)

    action = "updated" if existing_raw is not None else "created"
    before_hash = _hash(existing_raw) if existing_raw is not None else None
    after_hash = _hash(merged)
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"secret.{action}",
        target=name,
        before_hash=before_hash,
        after_hash=after_hash,
        detail_json=json.dumps({"path": str(target), "keys": sorted(merged.keys())}),
    )
    logger.info("Secret %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def delete_secret(name: str, *, actor: str = "web") -> WriteResult:
    """Delete a secret file from the user-scoped ``secrets.d/``.

    Files that only exist under ``/etc/ovid2/secrets.d/`` cannot be
    deleted through the web tier — the operator must remove them on
    disk. This mirrors the write policy.
    """
    _validate_name(name)
    target = _secrets_dir() / f"{name}.json"
    if not target.is_file():
        raise SecretError(f"Secret '{name}' not found under {_secrets_dir()}.")
    before_hash: str | None = None
    try:
        before_hash = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError, SecretError):
        before_hash = None
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="secret.deleted",
        target=name,
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Secret deleted %s at %s", name, target)
    return WriteResult(name=name, path=str(target), action="deleted")
