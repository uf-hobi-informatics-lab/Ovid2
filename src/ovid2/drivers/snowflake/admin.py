"""Snowflake workflow-rollback storage admin (snapshot / restore / drop tables).

A Snowflake address is ``DATABASE/SCHEMA``. A workflow snapshot of an
occupied output clones each table into a sibling schema
``<SCHEMA>_OVID2BK_<key>`` (zero-copy ``CREATE TABLE … CLONE``); restore
swaps the clone back; drop removes the tables; drop_backup drops the
sibling schema.

Every statement is parameterised: object names are bound with
``IDENTIFIER(%s)`` over a fully double-quoted name, never interpolated
into the SQL text. ``snowflake-connector-python`` is imported lazily so
the module is import-safe.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from ovid2.drivers._layout import workflow_backup_schema
from ovid2.drivers.snowflake.introspect import quoted_name

__all__ = ["drop_backup", "drop_tables", "restore_tables", "snapshot_tables"]

_SHOW_TABLES_SQL = (
    "SELECT TABLE_NAME FROM IDENTIFIER(%s) WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'"
)
_CREATE_SCHEMA_SQL = "CREATE SCHEMA IF NOT EXISTS IDENTIFIER(%s)"
_DROP_SCHEMA_SQL = "DROP SCHEMA IF EXISTS IDENTIFIER(%s)"
_CLONE_SQL = "CREATE OR REPLACE TABLE IDENTIFIER(%s) CLONE IDENTIFIER(%s)"
_DROP_TABLE_SQL = "DROP TABLE IF EXISTS IDENTIFIER(%s)"


@contextlib.contextmanager
def _cursor(args: dict[str, Any], secrets: dict[str, Any]) -> Iterator[Any]:
    import snowflake.connector as sf

    from ovid2.drivers.snowflake.probe import build_connect_kwargs

    conn = sf.connect(**build_connect_kwargs(args, secrets))
    try:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
    finally:
        conn.close()


def _tables_in(cur: Any, database: str, schema: str) -> dict[str, str]:
    """Return ``{upper_name: real_name}`` for the base tables in ``database.schema``."""
    cur.execute(_SHOW_TABLES_SQL, (quoted_name(database, "INFORMATION_SCHEMA", "TABLES"), schema))
    return {str(r[0]).upper(): str(r[0]) for r in cur.fetchall()}


def _resolve(names: dict[str, str], table: str) -> str | None:
    return names.get(table.upper())


def snapshot_tables(
    args: dict[str, Any],
    secrets: dict[str, Any],
    database: str,
    schema: str,
    tables: list[str],
    key: str,
) -> list[str]:
    """Clone ``tables`` into the backup schema; returns the tables cloned."""
    backup = workflow_backup_schema(schema, key)
    copied: list[str] = []
    with _cursor(args, secrets) as cur:
        cur.execute(_DROP_SCHEMA_SQL, (quoted_name(database, backup),))
        cur.execute(_CREATE_SCHEMA_SQL, (quoted_name(database, backup),))
        names = _tables_in(cur, database, schema)
        for table in tables:
            real = _resolve(names, table)
            if real is None:
                continue
            cur.execute(
                _CLONE_SQL,
                (quoted_name(database, backup, real), quoted_name(database, schema, real)),
            )
            copied.append(table)
    return copied


def restore_tables(
    args: dict[str, Any],
    secrets: dict[str, Any],
    database: str,
    schema: str,
    tables: list[str],
    key: str,
) -> list[str]:
    """Clone ``tables`` back from the backup schema; returns the tables restored."""
    backup = workflow_backup_schema(schema, key)
    restored: list[str] = []
    with _cursor(args, secrets) as cur:
        saved = _tables_in(cur, database, backup)
        for table in tables:
            real = _resolve(saved, table)
            if real is None:
                continue
            cur.execute(
                _CLONE_SQL,
                (quoted_name(database, schema, real), quoted_name(database, backup, real)),
            )
            restored.append(table)
    return restored


def drop_tables(
    args: dict[str, Any],
    secrets: dict[str, Any],
    database: str,
    schema: str,
    tables: list[str],
) -> list[str]:
    """Drop ``tables`` from ``database.schema``; returns the tables dropped."""
    dropped: list[str] = []
    with _cursor(args, secrets) as cur:
        names = _tables_in(cur, database, schema)
        for table in tables:
            real = _resolve(names, table)
            if real is None:
                continue
            cur.execute(_DROP_TABLE_SQL, (quoted_name(database, schema, real),))
            dropped.append(table)
    return dropped


def drop_backup(
    args: dict[str, Any],
    secrets: dict[str, Any],
    database: str,
    schema: str,
    key: str,
) -> None:
    """Drop the backup schema."""
    with _cursor(args, secrets) as cur:
        cur.execute(_DROP_SCHEMA_SQL, (quoted_name(database, workflow_backup_schema(schema, key)),))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
