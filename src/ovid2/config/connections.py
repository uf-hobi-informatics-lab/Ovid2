"""JSON-driven connection loader (W7).

Resolves connection names into concrete
:class:`ovid2.connections.base.BaseConnection` instances and
publishes the field-spec catalogue the web UI consumes.

Config file resolution order (identical to :mod:`ovid2.config.addresses`):

1. ``$OVID2_CONNECTIONS_DIR/<name>.json``
2. ``$XDG_CONFIG_HOME/ovid2/connections/<name>.json``
3. ``./connections/<name>.json`` (repo-local convenience for dev)
4. ``/etc/ovid2/connections/<name>.json``

Secret material is loaded via :mod:`ovid2.config.secrets` using the
optional ``secret_ref`` key on each connection file. Secrets stored
inline in the connection JSON are refused — the loader raises so
mis-configured files fail loudly rather than persisting plaintext
credentials into the config directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ovid2.config import secrets as _secrets_module
from ovid2.connections.registry import (
    CONNECTION_TYPE_MAP,
    connection_field_spec,
    list_connection_types,
    load_connection_class,
)


def _search_paths() -> list[Path]:
    """Return the ordered list of directories to search for connection JSONs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_CONNECTIONS_DIR")
    if override:
        paths.append(Path(override))
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg_cfg) / "ovid2" / "connections")
    paths.append(Path.cwd() / "connections")
    paths.append(Path("/etc/ovid2/connections"))
    return paths


def connections_dir() -> Path:
    """Return the user-scoped write target for connection files."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_cfg) / "ovid2" / "connections"


def locate_connection(name: str) -> Path | None:
    """Return the on-disk path of a connection JSON, or ``None`` when absent."""
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def list_available_connections() -> list[str]:
    """Return every connection name discoverable on the search path."""
    seen: set[str] = set()
    for directory in _search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".json" and not entry.stem.endswith("_sample"):
                seen.add(entry.stem)
    return sorted(seen)


def load_connection_config(name: str) -> dict[str, Any]:
    """Load a JSON connection config by name.

    Args:
        name: Filename stem (no ``.json``).

    Returns:
        Parsed dict guaranteed to have ``type`` and ``args``.

    Raises:
        FileNotFoundError: When no candidate directory contains the file.
        ValueError: When the JSON body is invalid or missing required keys.
    """
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as fh:
                config = json.load(fh)
            break
    else:
        tried = ", ".join(str(d) for d in _search_paths())
        raise FileNotFoundError(
            f"Connection config '{name}.json' not found in any of: {tried}"
        )

    if not isinstance(config, dict):
        raise ValueError(f"Connection config '{name}' must be a JSON object.")
    if "type" not in config:
        raise ValueError(f"Connection config '{name}' missing 'type'.")
    if config["type"] not in CONNECTION_TYPE_MAP:
        raise ValueError(
            f"Unknown connection type '{config['type']}' in '{name}'. "
            f"Registered: {sorted(CONNECTION_TYPE_MAP.keys())}."
        )
    config.setdefault("args", {})
    if not isinstance(config["args"], dict):
        raise ValueError(f"Connection '{name}' has non-object 'args'.")
    # Refuse inline secrets — the whole point of the split is that the
    # connection file itself never carries credential material.
    secret_field_names = {
        f["name"] for f in connection_field_spec(config["type"])["secrets"]
    }
    inline = sorted(set(config["args"]) & secret_field_names)
    if inline:
        raise ValueError(
            f"Connection '{name}' has secret fields inline in 'args': "
            f"{inline}. Move them into a secret file and set 'secret_ref'."
        )
    return config


def resolve_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return the secret payload referenced by ``config['secret_ref']``.

    Empty dict when the config has no ``secret_ref``. Raises when the
    reference exists but the file is missing.
    """
    ref = config.get("secret_ref")
    if not ref:
        return {}
    return _secrets_module.load_secret(str(ref))


def load_connection(name: str) -> Any:
    """Instantiate a :class:`BaseConnection` from its JSON config.

    Args:
        name: Filename stem.

    Returns:
        A concrete connector instance.
    """
    config = load_connection_config(name)
    cls = load_connection_class(str(config["type"]))
    return cls(args=dict(config.get("args") or {}), secrets=resolve_secrets(config))


__all__ = [
    "CONNECTION_TYPE_MAP",
    "connection_field_spec",
    "connections_dir",
    "list_available_connections",
    "list_connection_types",
    "load_connection",
    "load_connection_config",
    "locate_connection",
    "resolve_secrets",
]
