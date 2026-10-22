"""Connections — reusable, credential-heavy endpoints (W7).

A **Connection** describes *how to reach a system* (Snowflake account,
S3 bucket, Azure storage account, on-disc root, JDBC server, …). The
same connection can back many addresses; an :class:`Address` is a
lightweight view onto a Connection (the specific database/schema,
prefix, or path within the endpoint).

Connections are loaded from JSON files under
``$XDG_CONFIG_HOME/ovid2/connections/`` and their credential-shaped
fields are pulled from :mod:`ovid2.config.secrets` via ``secret_ref``.

The registry pattern mirrors :mod:`ovid2.core.addresses` — each
concrete type lives in its own module and is dotted-string-registered
in :data:`registry.CONNECTION_TYPE_MAP` so the loader can instantiate
by name without importing every connector.

This package lives at the top level (not under ``ovid2.core``) so the
web tier and MCP server can import it without violating the
"web must not import pipeline" contract test.
"""

from __future__ import annotations

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.connections.registry import (
    CONNECTION_TYPE_MAP,
    connection_field_spec,
    list_connection_types,
    load_connection_class,
)

__all__ = [
    "BaseConnection",
    "CONNECTION_TYPE_MAP",
    "ConnectionTestResult",
    "connection_field_spec",
    "list_connection_types",
    "load_connection_class",
]
