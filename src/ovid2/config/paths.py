"""XDG Base Directory helpers.

Single source of truth for the standard Ovid2 filesystem layout::

    $XDG_CONFIG_HOME/ovid2/           (user config)
    $XDG_CONFIG_HOME/ovid2/secrets.d/ (per-secret JSON files, mode 0600)
    $XDG_CONFIG_HOME/ovid2/configs/   (address configs)
    $XDG_CACHE_HOME/ovid2/            (cache: omop_cdm, downloaded jars, …)
    $XDG_STATE_HOME/ovid2/            (persistent state: scheduler.db, archive)
    $XDG_RUNTIME_DIR/ovid2/           (event logs, compose files, temp dirs)
    /etc/ovid2/                       (system-wide config; systemd install)
    /var/log/ovid2/                   (system-wide log archive)
    /var/run/ovid2/                   (system-wide runtime dir)

Each function returns a :class:`~pathlib.Path`. They **do not** create
directories; callers ``mkdir(parents=True, exist_ok=True)`` when they
need to write.

The helpers honour every applicable environment variable and never
raise for missing directories — they only compute the target path.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "cache_dir",
    "config_dir",
    "configs_dir",
    "runtime_dir",
    "secrets_dir",
    "state_dir",
    "system_config_dir",
    "system_log_dir",
    "system_runtime_dir",
]

_APP = "ovid2"


def _env_or_home(env_name: str, default_relative: str) -> Path:
    """Resolve an XDG env var, defaulting to ``~/<default_relative>``."""
    override = os.environ.get(env_name)
    if override:
        return Path(override)
    return Path.home() / default_relative


# ---------------------------------------------------------------------------
# User-scoped XDG paths
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/ovid2`` (default ``~/.config/ovid2``)."""
    return _env_or_home("XDG_CONFIG_HOME", ".config") / _APP


def secrets_dir() -> Path:
    """Return the user's ``secrets.d`` directory."""
    return config_dir() / "secrets.d"


def configs_dir() -> Path:
    """Return the user's address-configs directory."""
    return config_dir() / "configs"


def cache_dir() -> Path:
    """Return ``$XDG_CACHE_HOME/ovid2`` (default ``~/.cache/ovid2``)."""
    return _env_or_home("XDG_CACHE_HOME", ".cache") / _APP


def state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/ovid2`` (default ``~/.local/state/ovid2``)."""
    return _env_or_home("XDG_STATE_HOME", ".local/state") / _APP


def runtime_dir() -> Path:
    """Return ``$XDG_RUNTIME_DIR/ovid2`` (default ``/tmp/ovid2-<uid>``).

    ``XDG_RUNTIME_DIR`` has no home-based default per the spec, so we
    fall back to a per-uid ``/tmp`` directory.
    """
    override = os.environ.get("XDG_RUNTIME_DIR")
    if override:
        return Path(override) / _APP
    return Path(f"/tmp/{_APP}-{os.getuid()}")  # noqa: S108 — deliberate uid-scoped fallback


# ---------------------------------------------------------------------------
# System-scoped paths (populated by ``ovid2 service install``)
# ---------------------------------------------------------------------------


def system_config_dir() -> Path:
    """Return the system-wide config directory (``/etc/ovid2``)."""
    return Path("/etc/ovid2")


def system_log_dir() -> Path:
    """Return the system-wide log directory (``/var/log/ovid2``)."""
    return Path("/var/log/ovid2")


def system_runtime_dir() -> Path:
    """Return the system-wide runtime directory (``/var/run/ovid2``)."""
    return Path("/var/run/ovid2")
