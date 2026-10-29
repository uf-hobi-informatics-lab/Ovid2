"""Native ``PostgresDriver`` — Data Space browsing + table introspection.

Subclasses the legacy adapter for ``postgres`` (probe, read / write and
form field specs are unchanged) and adds the control-plane browse /
introspection hooks over ``psycopg2`` / ``psycopg`` (imported lazily):
``DATABASE / SCHEMA / TABLE`` listing from ``pg_database`` /
``information_schema`` / ``pg_class``, column metadata, a statistics
row estimate (``reltuples``), ``pg_total_relation_size`` and a
reconstructed definition. Install with ``pip install 'ovid2[postgres]'``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ovid2.drivers import _sql_introspect as sqli
from ovid2.drivers.base import StorageEntry, TableInfo, TableSample
from ovid2.drivers.legacy_adapter import build_legacy_driver_class
from ovid2.drivers.postgres.metadata import HIDDEN_SCHEMAS, connect_kwargs

__all__ = ["PostgresDriver"]


def _ddl(
    cur: Any,
    dialect: sqli.SqlDialect,
    database: str,
    schema: str,
    table: str,
    columns: Any,
    is_view: bool,
) -> str | None:
    del database
    if is_view:
        cur.execute(
            "SELECT pg_get_viewdef(to_regclass(%s), true)", (dialect.qualified(schema, table),)
        )
        row = cur.fetchone()
        if row and row[0]:
            return "CREATE VIEW " + dialect.qualified(schema, table) + " AS\n" + str(row[0])
    return sqli.reconstruct_ddl(dialect, schema, table, columns, is_view=is_view)


DIALECT = sqli.SqlDialect(
    name="postgres",
    quote_open='"',
    quote_close='"',
    paramstyle="format",
    databases_sql=(
        "SELECT datname FROM pg_database WHERE datistemplate = false AND datallowconn ORDER BY 1"
    ),
    schemas_sql="SELECT schema_name FROM information_schema.schemata ORDER BY 1",
    tables_sql=(
        "SELECT c.relname, "
        "CASE WHEN c.relkind IN ('v', 'm') THEN 'VIEW' ELSE 'TABLE' END, "
        "CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END, "
        "pg_total_relation_size(c.oid), NULL "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = {p} AND c.relkind IN ('r', 'v', 'm', 'p', 'f') ORDER BY 1"
    ),
    columns_sql=(
        "SELECT column_name, data_type, is_nullable, character_maximum_length, "
        "numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema = {p} AND table_name = {p} ORDER BY ordinal_position"
    ),
    hidden_schemas=frozenset(HIDDEN_SCHEMAS),
    ddl=_ddl,
)


class PostgresDriver(build_legacy_driver_class("postgres")):  # type: ignore[misc]
    """``postgres`` driver with Data Space browsing + introspection."""

    def _connector(self) -> Any:
        for module in ("psycopg2", "psycopg"):
            try:
                return importlib.import_module(module)
            except ImportError:
                continue
        raise NotImplementedError("psycopg is not installed (pip install 'ovid2[postgres]').")

    def _connect(self, database: str | None) -> Any:
        return self._connector().connect(
            **connect_kwargs(dict(self.args), dict(self.secrets), database)
        )

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List databases → schemas → tables."""
        self._connector()
        entries = sqli.list_level(DIALECT, self._connect, prefix)
        if not sqli.split_path(prefix):
            # Schemas whose names start with pg_temp / pg_toast_temp are noise.
            return entries
        return [e for e in entries if not e.name.startswith(("pg_temp", "pg_toast_temp"))]

    def describe_table(self, path: str) -> TableInfo:
        """Columns, row estimate, total size, definition."""
        self._connector()
        return sqli.describe_table(DIALECT, self._connect, path)

    def sample_rows(self, path: str, limit: int = 100) -> TableSample:
        """``SELECT * … LIMIT n``."""
        self._connector()
        return sqli.sample_rows(DIALECT, self._connect, path, limit)

    def iter_rows(
        self, path: str, limit: int | None = None, *, batch_size: int = 5000
    ) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
        """Stream the table."""
        self._connector()
        return sqli.iter_rows(DIALECT, self._connect, path, limit, batch_size=batch_size)

    def count_rows(self, path: str) -> int:
        """``SELECT COUNT(*)``."""
        self._connector()
        return sqli.count_rows(DIALECT, self._connect, path)


PostgresDriver.__name__ = "PostgresDriver"
PostgresDriver.__qualname__ = "PostgresDriver"

from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("postgres")
_register("postgres", "ovid2.drivers.postgres.driver.PostgresDriver")
