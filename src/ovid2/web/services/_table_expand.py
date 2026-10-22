"""Web-safe helper: does a copy/move source support the ``all`` sentinel?

``copy`` / ``move`` historically require an *explicit* ``--tables`` list
because the CLI has no ``all`` sentinel. Some sources, however, are
self-describing containers of tables (e.g. a Microsoft Access ``.accdb``
file). For those, ``all`` is meaningful and the executor can expand it at
run time via :meth:`ovid2.drivers.base.Driver.expand_all_tables`.

This module answers the *static* question — "can this source's driver
**type** expand ``all``?" — without resolving any secret, opening any
file, or starting a JVM (trust-model requirement:
``tests/contract/test_web_no_plaintext_secret_reads.py``). It maps an
address name to its driver *type* purely from on-disk config, then reads
the type's :meth:`Driver.supports_expand_all` class flag.
"""

from __future__ import annotations

import json

from ovid2.config import addresses as _addresses


def _driver_type_for_address(address_name: str) -> str | None:
    """Return the driver *type* for ``address_name`` (config-only).

    Reads only the address JSON (and, for connection-shape addresses,
    the referenced connection JSON's ``type``). Never resolves secrets.

    Returns:
        The driver type string (e.g. ``"access"``), or ``None`` when the
        address / connection cannot be resolved.
    """
    if not address_name.strip():
        return None
    try:
        config = _addresses.load_address_config(address_name)
    except (FileNotFoundError, ValueError):
        return None

    if config.get("_shape") == "legacy":
        src_name = config.get("src_name")
        return str(src_name) if src_name else None

    connection_name = config.get("connection")
    if not connection_name:
        return None
    try:
        from ovid2.config import connections as _connections

        path_conn = _connections.locate_connection(str(connection_name))
        if path_conn is None:
            return None
        with path_conn.open(encoding="utf-8") as fh:
            conn_payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    conn_type = str(conn_payload.get("type", ""))
    return _addresses.CONNECTION_TO_SRC_NAME.get(conn_type)


def source_supports_expand_all(address_name: str) -> bool:
    """Return ``True`` when the source address can safely accept ``all``.

    Resolves the address to its driver type and consults the driver
    type's static :meth:`Driver.supports_expand_all` flag. Any failure
    (unknown address, unregistered type) is treated as "does not
    support" so the caller keeps the historical rejection.

    Args:
        address_name: The saved-address name used as the copy/move source.

    Returns:
        ``True`` when ``all`` is expandable for this source, else
        ``False``.
    """
    driver_type = _driver_type_for_address(address_name)
    if not driver_type:
        return False
    try:
        from ovid2.drivers.registry import load_class

        driver_cls = load_class(driver_type)
    except (KeyError, ImportError):
        return False
    supports = getattr(driver_cls, "supports_expand_all", None)
    if supports is None:
        return False
    try:
        return bool(supports())
    except Exception:  # defensive: never let this crash submit
        return False


__all__ = ["source_supports_expand_all"]
