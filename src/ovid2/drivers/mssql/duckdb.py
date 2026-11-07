"""DuckDB data plane for the mssql driver (pymssql / pyodbc via DB-API).

There is no DuckDB SQL Server scanner, so reads stream ``SELECT *``
batches into scratch Parquet (all VARCHAR) and writes replace the
target table with ``NVARCHAR(MAX)`` columns filled by parameterised
``executemany`` batches. Connections come from
:meth:`ovid2.drivers.mssql.driver.MssqlDriver._connect`.

ovid2: allow-sql-fstring — identifiers are bracket-quoted with
:func:`_quote`; values are bound parameters.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import TYPE_CHECKING, Any, cast

from ovid2.core.engines._duck_column import ident
from ovid2.core.engines.duckdb_stager import (
    DuckDBStagingStrategy,
    local_scratch_dir,
    register_duckdb_strategy,
)
from ovid2.drivers._dbapi_duckdb import cursor_batches, rows_to_parquet, write_rows

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver


def _quote(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _location(driver: Driver) -> tuple[str, str]:
    from ovid2.drivers._layout import warehouse_db_schema

    database, schema = warehouse_db_schema(getattr(driver, "location", None))
    return database or str(driver.args.get("database") or "master"), schema or "dbo"


@register_duckdb_strategy
class DuckDBMssqlStrategy(DuckDBStagingStrategy):
    """mssql <-> DuckDB."""

    driver_type = "mssql"

    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> DuckFrame:
        """Spool ``SELECT * FROM [db].[schema].[table]`` into scratch Parquet."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        database, schema = _location(driver)
        table = file_name if file_name and "*" not in file_name else (table_name or file_name)
        target = os.path.join(local_scratch_dir(engine), f"{table}.parquet")
        conn = driver._connect(database)  # type: ignore[attr-defined]
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {_quote(database)}.{_quote(schema)}.{_quote(table)}")
            columns = [str(d[0]) for d in cur.description]
            rows_to_parquet(columns, cursor_batches(cur), target)
        finally:
            with contextlib.suppress(Exception):
                conn.close()
        return cast("DuckFrame", engine.read_parquet(target))

    def persist(
        self,
        df: DuckFrame,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Replace ``[db].[schema].[table]`` with the frame's rows (NVARCHAR(MAX))."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        database, schema = _location(driver)
        local = os.path.join(
            local_scratch_dir(engine), f"{table_name}-{uuid.uuid4().hex[:6]}.parquet"
        )
        view = f"__ovid2_ms_{uuid.uuid4().hex[:8]}"
        duck = engine.connection()
        text = df.relation.project(
            ", ".join(
                f"CAST(#{i} AS VARCHAR) AS {ident(c)}" for i, c in enumerate(df.columns, start=1)
            )
        )
        text.create_view(view, replace=True)
        try:
            duck.execute(f"COPY (SELECT * FROM {ident(view)}) TO '{local}' (FORMAT parquet)")
        finally:
            with contextlib.suppress(Exception):
                duck.execute(f"DROP VIEW IF EXISTS {ident(view)}")
        target = f"{_quote(database)}.{_quote(schema)}.{_quote(table_name.upper())}"
        create = (
            f"CREATE TABLE {target} ("
            + ", ".join(f"{_quote(c)} NVARCHAR(MAX) NULL" for c in df.columns)
            + ")"
        )
        name, _mod = driver._backend()  # type: ignore[attr-defined]
        conn = driver._connect(database)  # type: ignore[attr-defined]
        try:
            cur = conn.cursor()
            ph = "%s" if name == "pymssql" else "?"
            cur.execute(
                "DECLARE @s sysname = " + ph + "; "
                "IF SCHEMA_ID(@s) IS NULL BEGIN "
                "DECLARE @q nvarchar(400) = N'CREATE SCHEMA ' + QUOTENAME(@s); EXEC(@q); END",
                (schema,),
            )
            conn.commit()
            rows = write_rows(
                conn,
                parquet_path=local,
                target_sql=target,
                columns=list(df.columns),
                quote=_quote,
                placeholder="%s" if name == "pymssql" else "?",
                create_sql=create,
            )
            engine.add_rows_written(rows)
        finally:
            with contextlib.suppress(Exception):
                conn.close()
