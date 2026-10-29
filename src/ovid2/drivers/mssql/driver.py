"""Native ``MssqlDriver`` — Data Space browsing + table introspection.

Subclasses the legacy adapter for ``mssql`` (probe, field specs and the
data plane are unchanged) and adds ``DATABASE / SCHEMA / TABLE``
browsing, column metadata, partition-stat row estimates, sizes,
``modify_date``, a reconstructed definition (``OBJECT_DEFINITION`` for
views), samples (``SELECT TOP n``) and streaming, over ``pymssql`` or
``pyodbc`` (imported lazily). Install with ``pip install 'ovid2[mssql]'``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

from ovid2.drivers import _sql_introspect as sqli
from ovid2.drivers.base import StorageEntry, TableInfo, TableSample
from ovid2.drivers.legacy_adapter import build_legacy_driver_class

__all__ = ["MssqlDriver"]

_HIDDEN_SCHEMAS = frozenset(
    {
        "sys",
        "information_schema",
        "guest",
        "db_owner",
        "db_accessadmin",
        "db_securityadmin",
        "db_ddladmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_denydatareader",
        "db_denydatawriter",
    }
)


def _ddl(
    cur: Any,
    dialect: sqli.SqlDialect,
    database: str,
    schema: str,
    table: str,
    columns: Any,
    is_view: bool,
) -> str | None:
    if is_view:
        dialect.execute(
            cur,
            "SELECT OBJECT_DEFINITION(OBJECT_ID({p}))",
            (dialect.qualified(database, schema, table),),
        )
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    return sqli.reconstruct_ddl(dialect, schema, table, columns, is_view=is_view)


def _scope(dialect: sqli.SqlDialect, database: str) -> str:
    return dialect.quote(database)


def _dialect(paramstyle: str) -> sqli.SqlDialect:
    return sqli.SqlDialect(
        name="mssql",
        quote_open="[",
        quote_close="]",
        paramstyle=paramstyle,
        databases_sql=(
            "SELECT name FROM sys.databases WHERE database_id > 4 AND state = 0 "
            "AND HAS_DBACCESS(name) = 1 ORDER BY name"
        ),
        schemas_sql="SELECT name FROM {db}.sys.schemas ORDER BY name",
        tables_sql=(
            "SELECT o.name, CASE WHEN o.type = 'V' THEN 'VIEW' ELSE 'TABLE' END, "
            "(SELECT SUM(p.rows) FROM {db}.sys.partitions p "
            " WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)), "
            "(SELECT SUM(a.total_pages) * 8192 FROM {db}.sys.partitions p "
            " JOIN {db}.sys.allocation_units a ON a.container_id = p.partition_id "
            " WHERE p.object_id = o.object_id), "
            "o.modify_date "
            "FROM {db}.sys.objects o JOIN {db}.sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = {p} AND o.type IN ('U', 'V') ORDER BY o.name"
        ),
        columns_sql=(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
            "NUMERIC_PRECISION, NUMERIC_SCALE FROM {db}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = {p} AND TABLE_NAME = {p} ORDER BY ORDINAL_POSITION"
        ),
        hidden_schemas=_HIDDEN_SCHEMAS,
        hidden_databases=frozenset({"master", "tempdb", "model", "msdb"}),
        top_syntax=True,
        ddl=_ddl,
        per_database_connect=False,
        cross_database_names=True,
        schema_scoped_sql=_scope,
    )


class MssqlDriver(build_legacy_driver_class("mssql")):  # type: ignore[misc]
    """``mssql`` driver with Data Space browsing + introspection."""

    def _backend(self) -> tuple[str, Any]:
        for module in ("pymssql", "pyodbc"):
            try:
                return module, importlib.import_module(module)
            except ImportError:
                continue
        raise NotImplementedError("pymssql / pyodbc is not installed (pip install 'ovid2[mssql]').")

    def _dialect(self) -> sqli.SqlDialect:
        name, _mod = self._backend()
        return _dialect("format" if name == "pymssql" else "qmark")

    def _connect(self, database: str | None) -> Any:
        name, mod = self._backend()
        host = str(self.args.get("host") or "")
        port = int(self.args.get("port") or 1433)
        user = str(self.args.get("user") or "")
        password = str(self.secrets.get("password") or "")
        db = str(database or self.args.get("database") or "master")
        if name == "pymssql":
            return mod.connect(
                server=host, port=port, user=user, password=password, database=db, login_timeout=15
            )
        encrypt = str(self.args.get("encrypt") or "yes")
        trust = str(self.args.get("trust_server_certificate") or "no")
        dsn = ";".join(
            [
                "DRIVER={ODBC Driver 18 for SQL Server}",
                f"SERVER={host},{port}",
                f"DATABASE={db}",
                f"UID={user}",
                f"PWD={password}",
                f"Encrypt={encrypt}",
                f"TrustServerCertificate={trust}",
            ]
        )
        return mod.connect(dsn, timeout=15)

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List databases → schemas → tables."""
        return sqli.list_level(self._dialect(), self._connect, prefix)

    def describe_table(self, path: str) -> TableInfo:
        """Columns, partition-stat row count, size, ``modify_date``, definition."""
        return sqli.describe_table(self._dialect(), self._connect, path)

    def sample_rows(self, path: str, limit: int = 100) -> TableSample:
        """``SELECT TOP n *``."""
        return sqli.sample_rows(self._dialect(), self._connect, path, limit)

    def iter_rows(
        self, path: str, limit: int | None = None, *, batch_size: int = 5000
    ) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
        """Stream the table."""
        return sqli.iter_rows(self._dialect(), self._connect, path, limit, batch_size=batch_size)

    def count_rows(self, path: str) -> int:
        """``SELECT COUNT(*)``."""
        return sqli.count_rows(self._dialect(), self._connect, path)


MssqlDriver.__name__ = "MssqlDriver"
MssqlDriver.__qualname__ = "MssqlDriver"

from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("mssql")
_register("mssql", "ovid2.drivers.mssql.driver.MssqlDriver")
