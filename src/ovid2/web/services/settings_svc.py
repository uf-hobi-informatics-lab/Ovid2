"""Global settings service (W7).

Runtime preferences persisted to ``$XDG_CONFIG_HOME/ovid2/settings.json``
(mode ``0o644``). No secrets. The file is optional — if missing the
service returns the model defaults, and callers can call
:func:`save_settings` to materialise it.

The consumers (:mod:`ovid2.web.worker`, :mod:`ovid2.web.settings`) call
:func:`get_settings` per request; a small mtime-based cache keeps that
cheap while still surfacing edits within one poll cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from ovid2.config import paths as _paths
from ovid2.web import db as web_db
from ovid2.web.models import GlobalSettings

logger = logging.getLogger(__name__)

__all__ = [
    "SettingsError",
    "get_settings",
    "invalidate_cache",
    "save_settings",
    "settings_path",
]


class SettingsError(ValueError):
    """Raised when a settings payload cannot be persisted."""


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": None, "value": None}


def settings_path() -> Path:
    """Return the on-disk path of the settings JSON file."""
    override = os.environ.get("OVID2_SETTINGS_FILE")
    if override:
        return Path(override)
    return _paths.config_dir() / "settings.json"


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def invalidate_cache() -> None:
    """Drop the in-process mtime cache (used by tests)."""
    with _CACHE_LOCK:
        _CACHE["mtime"] = None
        _CACHE["value"] = None


def get_settings() -> GlobalSettings:
    """Return the merged settings — disk-backed values, defaults for the rest.

    Cache: when ``settings.json`` has not changed since the last read
    the previously-parsed :class:`GlobalSettings` is returned. This
    keeps hot paths (per-request reads on every FastAPI call) cheap.
    """
    path = settings_path()
    try:
        stat = path.stat()
    except OSError:
        stat = None

    with _CACHE_LOCK:
        cached_mtime = _CACHE["mtime"]
        cached_value = _CACHE["value"]
        current_mtime = stat.st_mtime_ns if stat else None
        if isinstance(cached_value, GlobalSettings) and cached_mtime == current_mtime:
            return cached_value

    if stat is None:
        value = GlobalSettings()
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("settings.json unreadable at %s: %s", path, exc)
            value = GlobalSettings()
        else:
            try:
                value = GlobalSettings.model_validate(payload)
            except Exception as exc:
                logger.warning("settings.json failed validation: %s", exc)
                value = GlobalSettings()

    with _CACHE_LOCK:
        _CACHE["mtime"] = current_mtime
        _CACHE["value"] = value
    return value


def _validate_admin_storage(payload: GlobalSettings) -> None:
    """Reject an ``admin.storage_account`` that names no known connection.

    Mirrors the shape of the engine reference guard: an empty / ``None``
    value is always accepted (it means "use the local scheduler.db"),
    but a non-empty name must resolve to a connection discoverable by
    :func:`connections_svc.list_connections`. Raises
    :class:`SettingsError` (surfaced as a 400 / ``?error=`` on the
    settings form) otherwise.
    """
    name = (payload.admin.storage_account or "").strip()
    if not name:
        return
    from ovid2.web.services import connections_svc

    known = {c.name for c in connections_svc.list_connections()}
    if name not in known:
        raise SettingsError(f"admin.storage_account references unknown connection {name!r}")


def save_settings(payload: GlobalSettings, *, actor: str = "web") -> GlobalSettings:
    """Overwrite ``settings.json`` with the supplied model.

    Args:
        payload: The full settings object.
        actor: Origin label recorded in ``web_audit``.

    Returns:
        The persisted settings (re-fetched via :func:`get_settings`).

    Raises:
        SettingsError: When the target path cannot be written, or when
            ``admin.storage_account`` names an unknown connection.
    """
    _validate_admin_storage(payload)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    existing_hash: str | None = None
    if path.is_file():
        try:
            existing_hash = _hash(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            existing_hash = None
    doc = payload.model_dump(mode="json")
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    except OSError as exc:
        raise SettingsError(f"Could not write {path}: {exc}") from exc
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="settings.saved",
        target="settings",
        before_hash=existing_hash,
        after_hash=_hash(doc),
        detail_json=json.dumps({"path": str(path)}),
    )
    invalidate_cache()
    return get_settings()
