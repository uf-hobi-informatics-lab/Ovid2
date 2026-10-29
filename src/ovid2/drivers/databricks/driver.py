"""Native ``DatabricksDriver`` — Data Space browsing + table introspection.

Subclasses the legacy adapter for ``databricks`` (probe and field specs
unchanged) and adds ``CATALOG / SCHEMA / TABLE`` browsing through the
Unity Catalog ``information_schema``, column metadata, ``last_altered``,
``SHOW CREATE TABLE`` definitions, samples and streaming over
``databricks-sql-connector`` (imported lazily). Install with
``pip install 'ovid2[databricks]'``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from ovid2.drivers import _sql_introspect as sqli
from ovid2.drivers.base import StorageEntry, TableInfo, TableSample
from ovid2.drivers.legacy_adapter import build_legacy_driver_class

__all__ = ["DatabricksDriver"]


def _ddl(
    cur: Any,
    dialect: sqli.SqlDialect,
    database: str,
    schema: str,
    table: str,
    columns: Any,
    is_view: bool,
) -> str | None:
    with contextlib.suppress(Exception):
        cur.execute("SHOW CREATE TABLE " + dialect.qualified(database, schema, table))
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    return sqli.reconstruct_ddl(dialect, schema, table, columns, is_view=is_view)


def _scope(dialect: sqli.SqlDialect, database: str) -> str:
    return dialect.quote(database)


DIALECT = sqli.SqlDialect(
    name="databricks",
    quote_open="`",
    quote_close="`",
    paramstyle="named",
    databases_sql="SELECT catalog_name FROM system.information_schema.catalogs ORDER BY 1",
    schemas_sql="SELECT schema_name FROM {db}.information_schema.schemata ORDER BY 1",
    tables_sql=(
        "SELECT table_name, table_type, NULL, NULL, last_altered "
        "FROM {db}.information_schema.tables WHERE table_schema = {p} ORDER BY 1"
    ),
    columns_sql=(
        "SELECT column_name, full_data_type, is_nullable, character_maximum_length, "
        "numeric_precision, numeric_scale FROM {db}.information_schema.columns "
        "WHERE table_schema = {p} AND table_name = {p} ORDER BY ordinal_position"
    ),
    hidden_schemas=frozenset({"information_schema"}),
    hidden_databases=frozenset({"system", "__databricks_internal"}),
    ddl=_ddl,
    per_database_connect=False,
    cross_database_names=True,
    schema_scoped_sql=_scope,
)


class DatabricksDriver(build_legacy_driver_class("databricks")):  # type: ignore[misc]
    """``databricks`` driver with Data Space browsing + introspection."""

    def _sql(self) -> Any:
        try:
            from databricks import sql  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NotImplementedError(
                "databricks-sql-connector is not installed (pip install 'ovid2[databricks]')."
            ) from exc
        return sql

    def _connect(self, database: str | None) -> Any:
        sql = self._sql()
        kwargs: dict[str, Any] = {
            "server_hostname": str(self.args.get("host") or "").replace("https://", "").rstrip("/"),
            "http_path": str(self.args.get("http_path") or ""),
            "access_token": str(self.secrets.get("token") or ""),
        }
        catalog = database or self.args.get("catalog")
        if catalog:
            kwargs["catalog"] = str(catalog)
        return sql.connect(**kwargs)

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List catalogs → schemas → tables."""
        self._sql()
        return sqli.list_level(DIALECT, self._connect, prefix)

    def describe_table(self, path: str) -> TableInfo:
        """Columns, ``last_altered``, ``SHOW CREATE TABLE``."""
        self._sql()
        return sqli.describe_table(DIALECT, self._connect, path)

    def sample_rows(self, path: str, limit: int = 100) -> TableSample:
        """``SELECT * … LIMIT n``."""
        self._sql()
        return sqli.sample_rows(DIALECT, self._connect, path, limit)

    def iter_rows(
        self, path: str, limit: int | None = None, *, batch_size: int = 5000
    ) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
        """Stream the table."""
        self._sql()
        return sqli.iter_rows(DIALECT, self._connect, path, limit, batch_size=batch_size)

    def count_rows(self, path: str) -> int:
        """``SELECT COUNT(*)``."""
        self._sql()
        return sqli.count_rows(DIALECT, self._connect, path)


DatabricksDriver.__name__ = "DatabricksDriver"
DatabricksDriver.__qualname__ = "DatabricksDriver"

from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("databricks")
_register("databricks", "ovid2.drivers.databricks.driver.DatabricksDriver")
