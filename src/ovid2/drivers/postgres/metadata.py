"""Import-safe helpers for the native ``postgres`` driver (no SDK imports)."""

from __future__ import annotations

from typing import Any

__all__ = ["HIDDEN_SCHEMAS", "connect_kwargs"]

#: System schemas never shown in the Data Space.
HIDDEN_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast"})


def connect_kwargs(
    args: dict[str, Any], secrets: dict[str, Any], database: str | None
) -> dict[str, Any]:
    """Return ``psycopg`` / ``psycopg2`` ``connect()`` kwargs.

    ``database`` (a Data Space path segment) overrides the connection's
    default database — a Postgres connection is bound to one database.
    """
    kwargs: dict[str, Any] = {
        "host": str(args.get("host") or ""),
        "port": int(args.get("port") or 5432),
        "dbname": str(database or args.get("database") or "postgres"),
        "user": str(args.get("user") or ""),
        "connect_timeout": 15,
    }
    if secrets.get("password") is not None:
        kwargs["password"] = secrets["password"]
    if args.get("sslmode"):
        kwargs["sslmode"] = args["sslmode"]
    return kwargs
