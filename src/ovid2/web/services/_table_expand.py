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

It also performs the *concrete* draft/submit-time expansion
(:func:`expand_source_tables`) by delegating to the
``ovid2 tools list-source-tables`` executor broker, so a reviewed draft
lists the real tables (largest first) instead of the opaque ``all``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ovid2.config import addresses as _addresses

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class SourceTablesExpansion:
    """Outcome of a draft/submit-time ``all`` expansion.

    Attributes:
        code: ``ok`` (``tables`` is the concrete roster) |
            ``unsupported`` (the driver type cannot enumerate tables) |
            ``unavailable`` (it can, but not from the web host — the copy
            executor re-expands ``all`` at run time) | ``error`` (a real
            listing failure: missing batch folder, bad schema, …).
        tables: Concrete ``--tables`` specs, largest first when sizes
            are known (``LOGICAL`` or ``LOGICAL=SOURCE``).
        sizes: ``{source_key: bytes}`` for the tables whose size is known.
        message: Human/LLM-readable reason when ``code != 'ok'``.
    """

    code: str
    tables: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)
    message: str = ""


def expand_source_tables(
    address_name: str,
    *,
    pipeline: str,
    batch_id: str,
) -> SourceTablesExpansion:
    """Expand a copy/move ``all`` request to the source's concrete tables.

    Delegates to the executor broker (``ovid2 tools list-source-tables``)
    so the web process never resolves a credential. The broker lists the
    source (Snowflake ``SHOW TABLES``, disc ``scandir``, object-store
    listing, Access catalogue) and returns the roster ordered largest
    first.

    Args:
        address_name: Saved source-address name.
        pipeline: Pipeline name (folder sources resolve the batch dir).
        batch_id: Batch identifier.

    Returns:
        A :class:`SourceTablesExpansion`. Never raises: broker failures
        map to ``unavailable`` (static support) or ``error``.
    """
    if not source_supports_expand_all(address_name):
        return SourceTablesExpansion(
            code="unsupported",
            message=(
                f"source '{address_name}' cannot list its own tables "
                f"(driver type '{_driver_type_for_address(address_name) or 'unknown'}')."
            ),
        )
    from ovid2.web.services._broker import BrokerError, run_tool_json
    from ovid2.web.services.storage_browse_svc import _browse_deadline_seconds

    try:
        result = run_tool_json(
            [
                "list-source-tables",
                address_name,
                "--pipeline",
                pipeline,
                "--batch_id",
                batch_id,
            ],
            timeout=_browse_deadline_seconds(),
        )
    except BrokerError as exc:
        logger.warning("list-source-tables for '%s' failed: %s", address_name, exc)
        return SourceTablesExpansion(code="unavailable", message=str(exc))

    code = str(result.get("code") or ("ok" if result.get("ok") else "error"))
    tables = [str(t) for t in (result.get("tables") or []) if str(t).strip()]
    if code == "ok" and not tables:
        code = "error"
    raw_sizes = result.get("sizes") or {}
    sizes = (
        {str(k): int(v) for k, v in raw_sizes.items() if isinstance(v, int)}
        if isinstance(raw_sizes, dict)
        else {}
    )
    return SourceTablesExpansion(
        code=code,
        tables=tables if code == "ok" else [],
        sizes=sizes,
        message=str(result.get("message") or ""),
    )


__all__ = ["SourceTablesExpansion", "expand_source_tables", "source_supports_expand_all"]
