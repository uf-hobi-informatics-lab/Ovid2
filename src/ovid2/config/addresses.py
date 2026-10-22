"""JSON-driven address configuration loader.

Loads address definitions from ``configs/{name}.json`` and resolves them
into concrete :class:`ovid2.core.addresses.base.BaseAddress` instances.

Two file shapes are supported (W7):

1. **Legacy** — ``{"src_name": "<type>", "src_args": {...}}``.
   Credentials and location are mixed together. Left in place so the
   24 pre-W7 pipeline configs continue to load without a data migration.

2. **New** — ``{"connection": "<name>", "location": {...}}``. The
   named connection is dereferenced through
   :func:`ovid2.config.connections.load_connection` and merged with the
   address's ``location`` payload via
   :meth:`BaseConnection.as_address_config`. Credentials live in the
   connection's ``secret_ref`` under ``$XDG_CONFIG_HOME/ovid2/secrets.d/``
   with mode ``0o600``.

Snowflake configs still go through the four-tier resolution
(CLI override → address JSON → per-pipeline default → global default).
The tiers are applied *after* the connection dereference so a
connection-shaped address remains overridable at run time.

Config file resolution order:

1. ``$OVID2_CONFIGS_DIR/<name>.json``
2. ``$XDG_CONFIG_HOME/ovid2/configs/<name>.json``
3. ``./configs/<name>.json`` (repo-local convenience for dev)
4. ``/etc/ovid2/configs/<name>.json``
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ovid2.core.addresses.base import BaseAddress

logger = logging.getLogger(__name__)

# Registered address types → dotted class paths.
ADDRESS_TYPE_MAP: dict[str, str] = {
    "disc": "ovid2.drivers.disc.DiscAddress",
    "snowflake": "ovid2.drivers.snowflake.SnowflakeAddress",
    "azure_blob": "ovid2.drivers.azure_blob.AzureBlobAddress",
    "s3": "ovid2.drivers.s3.S3Address",
    "access": "ovid2.drivers.access.AccessAddress",
}

#: Connection type → matching legacy ``src_name`` for the address class.
#:
#: When the loader dereferences a new-shape address it needs to know
#: which :data:`ADDRESS_TYPE_MAP` entry to instantiate. This mapping is
#: the bridge; multiple connection types may share one address class
#: (e.g. every object-store connector eventually resolves to a
#: hypothetical ``ObjectStoreAddress``).
CONNECTION_TO_SRC_NAME: dict[str, str] = {
    "disc": "disc",
    "snowflake": "snowflake",
    "azure_blob": "azure_blob",
    "s3": "s3",
    "access": "access",
}


def _config_search_paths() -> list[Path]:
    """Return the ordered list of directories to search for JSON configs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_CONFIGS_DIR")
    if override:
        paths.append(Path(override))

    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg_cfg) / "ovid2" / "configs")
    paths.append(Path.cwd() / "configs")
    paths.append(Path("/etc/ovid2/configs"))
    return paths


def load_address_config(address_name: str) -> dict[str, Any]:
    """Load a JSON address config by name.

    Args:
        address_name: Filename stem (no ``.json`` suffix).

    Returns:
        Parsed config dict. Two shapes:

        - Legacy: ``{"src_name": ..., "src_args": {...}}``.
        - New:    ``{"connection": ..., "location": {...}}``.

        A ``_shape`` key is inserted (``"legacy"`` or ``"connection"``)
        so callers can branch without re-checking the keys.

    Raises:
        FileNotFoundError: If no candidate directory contains the config.
        ValueError: If required keys are missing.
    """
    for directory in _config_search_paths():
        candidate = directory / f"{address_name}.json"
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as fh:
                config = json.load(fh)
            break
    else:
        tried = ", ".join(str(d) for d in _config_search_paths())
        raise FileNotFoundError(
            f"Address config '{address_name}.json' not found in any of: {tried}"
        )

    if not isinstance(config, dict):
        raise ValueError(f"Address config '{address_name}' is not a JSON object.")

    if "connection" in config:
        config.setdefault("location", {})
        if not isinstance(config["location"], dict):
            raise ValueError(f"Address config '{address_name}' has non-object 'location'.")
        config["_shape"] = "connection"
        return config

    if "src_name" not in config:
        raise ValueError(f"Address config '{address_name}' missing 'src_name'.")
    if "src_args" not in config:
        raise ValueError(f"Address config '{address_name}' missing 'src_args'.")
    config["_shape"] = "legacy"
    return config


def load_address(
    address_name: str,
    sf_overrides: dict[str, str] | None = None,
    pipeline: str | None = None,
    cdm_name: str | None = None,
) -> BaseAddress:
    """Resolve a named JSON config into a :class:`BaseAddress` instance.

    Args:
        address_name: Filename stem of a config in ``configs/``.
        sf_overrides: Optional CLI-provided Snowflake overrides.
        pipeline: Optional pipeline name (used for per-pipeline Snowflake defaults).
        cdm_name: Optional CDM name (e.g. ``pcornet_7_0``). Threaded
            into the address's config dict as the ``cdm`` key so the
            Snowflake writer can pick the right typed DDL. Introduced
            by phase C1.

    Returns:
        The instantiated address.

    Raises:
        ValueError: If the resolved type is unknown.
    """
    config = load_address_config(address_name)

    if config.get("_shape") == "connection":
        src_name, src_args = _resolve_connection_shape(address_name, config)
    else:
        src_name = str(config["src_name"])
        src_args = dict(config["src_args"])

    if src_name == "snowflake":
        src_args = _resolve_snowflake_config(src_args, sf_overrides, pipeline)

    if cdm_name:
        src_args["cdm"] = cdm_name

    if src_name not in ADDRESS_TYPE_MAP:
        raise ValueError(
            f"Unknown address type '{src_name}' in config '{address_name}'. "
            f"Valid types: {', '.join(ADDRESS_TYPE_MAP.keys())}"
        )

    module_path, class_name = ADDRESS_TYPE_MAP[src_name].rsplit(".", 1)
    module = importlib.import_module(module_path)
    address_class = getattr(module, class_name)
    address: BaseAddress = address_class(src_args)
    return address


def load_address_from_connection(
    connection_name: str,
    location: dict[str, Any] | None = None,
    *,
    sf_overrides: dict[str, str] | None = None,
    pipeline: str | None = None,
    cdm_name: str | None = None,
) -> BaseAddress:
    """Resolve an *inline* connection + location into a :class:`BaseAddress`.

    This is the run-time-selection counterpart to :func:`load_address`:
    it builds an address from a storage account (``connection_name``) and
    a browsed/typed ``location`` (e.g. ``{"database": ..., "schema": ...}``
    or ``{"path": "DB/SCHEMA"}``) **without** requiring a saved JSON
    config on disc. It is used by the data-health / EDC-checks surfaces
    so one reusable run can target any database of a CDM — the operator
    picks the storage account, database, and schema at submit time and
    nothing is baked into a per-partner pipeline package.

    Args:
        connection_name: Storage-account (connection) config name.
        location: Per-address location dict. ``None`` → empty location.
        sf_overrides: Optional CLI-provided Snowflake overrides.
        pipeline: Optional pipeline label (Snowflake per-pipeline default).
        cdm_name: Optional CDM name threaded into ``src_args['cdm']``.

    Returns:
        The instantiated address.

    Raises:
        ValueError: If the connection type has no matching address class.
    """
    synthetic = f"<inline:{connection_name}>"
    src_name, src_args = _resolve_connection_shape(
        synthetic, {"connection": connection_name, "location": dict(location or {})}
    )
    if src_name == "snowflake":
        src_args = _resolve_snowflake_config(src_args, sf_overrides, pipeline)
    if cdm_name:
        src_args["cdm"] = cdm_name
    if src_name not in ADDRESS_TYPE_MAP:
        raise ValueError(
            f"Unknown address type '{src_name}' for connection '{connection_name}'. "
            f"Valid types: {', '.join(ADDRESS_TYPE_MAP.keys())}"
        )
    module_path, class_name = ADDRESS_TYPE_MAP[src_name].rsplit(".", 1)
    module = importlib.import_module(module_path)
    address_class = getattr(module, class_name)
    address: BaseAddress = address_class(src_args)
    return address


def _resolve_connection_shape(
    address_name: str,
    config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Dereference a new-shape address into ``(src_name, src_args)``.

    Uses :mod:`ovid2.config.connections` so the address module does not
    depend on the concrete connector classes at import time.
    """
    # Local import: avoids a circular ``config -> core -> config`` chain
    # in edge cases where a connector module imports address helpers.
    from ovid2.config import connections as _connections

    connection_name = str(config["connection"])
    location: dict[str, Any] = dict(config.get("location") or {})

    try:
        connection = _connections.load_connection(connection_name)
    except FileNotFoundError as exc:
        raise ValueError(
            f"Address '{address_name}' references missing connection '{connection_name}'."
        ) from exc

    conn_type = str(connection.type_name)
    if conn_type not in CONNECTION_TO_SRC_NAME:
        raise ValueError(
            f"Address '{address_name}' targets connection type '{conn_type}' "
            "which has no matching legacy address class yet."
        )
    src_name = CONNECTION_TO_SRC_NAME[conn_type]
    src_args = connection.as_address_config(location)
    logger.debug(
        "Resolved connection-shape address %s via connection=%s type=%s",
        address_name,
        connection_name,
        conn_type,
    )
    return src_name, src_args


def _resolve_snowflake_config(
    src_args: dict[str, Any],
    sf_overrides: dict[str, str] | None,
    pipeline: str | None,
) -> dict[str, Any]:
    """Apply the four-tier Snowflake config resolution."""
    from ovid2.config.settings import DEFAULT_SF_ACCOUNT

    merged: dict[str, Any] = dict(src_args)

    # Tier 3: pipeline default.
    if not merged.get("database") and pipeline:
        try:
            settings_module = importlib.import_module(
                f"ovid2_pipelines.{pipeline}.pipeline_settings"
            )
            pipeline_db = getattr(settings_module, "DEFAULT_PIPELINE_DB_NAME", None)
            if pipeline_db:
                merged["database"] = pipeline_db
        except ModuleNotFoundError:
            pass

    # Tier 4: global default.
    if not merged.get("account") and DEFAULT_SF_ACCOUNT:
        merged["account"] = DEFAULT_SF_ACCOUNT

    # Tier 1: CLI overrides (last so they win).
    if sf_overrides:
        merged.update(sf_overrides)
    return merged


def list_available_addresses() -> list[str]:
    """List all address config names found in the search path.

    Returns:
        Sorted, de-duplicated list of names (filename stems).
    """
    seen: set[str] = set()
    for directory in _config_search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".json" and not entry.stem.endswith("_sample"):
                seen.add(entry.stem)
    return sorted(seen)
