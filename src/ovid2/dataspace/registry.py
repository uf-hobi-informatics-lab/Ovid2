"""Dimension registry — the connection-type family view over ODS.

A **dimension** is a connection-type name (``disc``, ``snowflake``,
``s3``, ``azure_blob``, ``gcs``, ``databricks``, ``postgres``,
``mssql``). This module publishes:

- :func:`list_dimensions` — sorted list of registered dimensions.
- :func:`dimension_for_address` — the dimension of one address
  config (works for both connection-shape and legacy-shape addresses).
- :func:`list_addresses_by_dimension` — every address config that
  falls under a dimension.

The mapping is intentionally derived from
:mod:`ovid2.connections.registry` at import time so a new connector
type automatically becomes a new dimension. The contract test
``tests/contract/test_ods_dimensions_complete.py`` locks that.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ovid2.config import addresses as _address_config
from ovid2.connections.registry import CONNECTION_TYPE_MAP

logger = logging.getLogger(__name__)

__all__ = [
    "dimension_for_address",
    "list_addresses_by_dimension",
    "list_dimensions",
]


def list_dimensions() -> list[str]:
    """Return the sorted list of every registered ODS dimension."""
    return sorted(CONNECTION_TYPE_MAP.keys())


def _address_config_shape(name: str) -> tuple[str, dict[str, Any]] | None:
    """Return ``(shape, raw_config)`` for an address, or ``None`` if missing.

    ``shape`` is ``"legacy"`` or ``"connection"``. The raw config dict
    is returned unmodified (with the ``_shape`` marker inserted by
    :func:`ovid2.config.addresses.load_address_config`).
    """
    try:
        config = _address_config.load_address_config(name)
    except FileNotFoundError:
        return None
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("Address config '%s' failed to parse: %s", name, exc)
        return None
    return config.get("_shape", "legacy"), config


def dimension_for_address(name: str) -> str | None:
    """Return the dimension name for an address config.

    Args:
        name: Address config filename stem (e.g. ``"disc_input"``).

    Returns:
        The dimension name (matching a key of
        :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP`), or
        ``None`` when the address does not exist or its shape cannot
        be resolved.
    """
    parsed = _address_config_shape(name)
    if parsed is None:
        return None
    shape, config = parsed

    if shape == "connection":
        connection_name = str(config.get("connection", "")).strip()
        if not connection_name:
            return None
        # Avoid instantiating the connection (some connectors' constructors
        # do work) — read the on-disk JSON directly.
        from ovid2.config import connections as _connection_config

        try:
            conn_cfg = _connection_config.load_connection_config(connection_name)
        except FileNotFoundError:
            return None
        conn_type = str(conn_cfg.get("type", "")).strip()
        if conn_type in CONNECTION_TYPE_MAP:
            return conn_type
        return None

    # Legacy shape — the ``src_name`` field lines up with a connection type
    # today via ``CONNECTION_TO_SRC_NAME`` (which is 1:1 in v1).
    src_name = str(config.get("src_name", "")).strip()
    if src_name in CONNECTION_TYPE_MAP:
        return src_name
    return None


def list_addresses_by_dimension(dimension: str) -> list[str]:
    """Return every address config under ``dimension``, sorted by name.

    Args:
        dimension: One of :func:`list_dimensions`.

    Returns:
        Sorted list of address config filename stems.

    Raises:
        KeyError: When ``dimension`` is not a registered dimension.
    """
    if dimension not in CONNECTION_TYPE_MAP:
        raise KeyError(
            f"Unknown ODS dimension '{dimension}'. "
            f"Registered: {list_dimensions()}."
        )
    matches: list[str] = []
    for address_name in _address_config.list_available_addresses():
        if dimension_for_address(address_name) == dimension:
            matches.append(address_name)
    return sorted(matches)


def _address_config_paths() -> list[Path]:
    """Return search-path directories for address configs (test hook)."""
    return _address_config._config_search_paths()
