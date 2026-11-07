"""DuckDB data plane for the postgres driver (DuckDB ``postgres`` extension).

The database is ``ATTACH``-ed read/write through the ``postgres``
extension (pre-installed in the image), so reads are pushed down /
streamed natively and writes are a single ``CREATE OR REPLACE TABLE …
AS SELECT`` executed by DuckDB straight into Postgres. The libpq
connection string is assembled from
:func:`ovid2.drivers.postgres.metadata.connect_kwargs`; the password is
held in a session-scoped DuckDB ``TEMPORARY SECRET``.

ovid2: allow-sql-fstring — statements interpolate identifiers quoted by
:func:`~ovid2.core.engines._duck_column.ident` and engine-generated
attachment / secret names; credentials are escaped literals in a TEMPORARY secret (never logged).
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any, cast

from ovid2.core.engines._duck_column import ident
from ovid2.core.engines.duckdb_stager import DuckDBStagingStrategy, register_duckdb_strategy

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver


def _location(driver: Driver) -> tuple[str, str]:
    from ovid2.drivers._layout import warehouse_db_schema

    database, schema = warehouse_db_schema(getattr(driver, "location", None))
    return database or str(driver.args.get("database") or "postgres"), schema or "public"


def attach(engine: Any, driver: Driver) -> tuple[str, str]:
    """ATTACH the driver's database once per engine; return ``(alias, schema)``."""
    from ovid2.drivers.postgres.metadata import connect_kwargs

    database, schema = _location(driver)
    kwargs = connect_kwargs(dict(driver.args), dict(driver.secrets), database)
    key = ("postgres", kwargs["host"], kwargs["port"], kwargs["dbname"], kwargs["user"])
    cache: dict[Any, str] = engine.__dict__.setdefault("_pg_attached", {})
    # ATTACH is database-wide (every table-worker cursor sees it); the
    # lock keeps parallel tables from attaching the same database twice.
    with engine._lock:
        return _attach_locked(engine, cache, key, kwargs, schema)


def _attach_locked(
    engine: Any, cache: dict[Any, str], key: Any, kwargs: dict[str, Any], schema: str
) -> tuple[str, str]:
    """Body of :func:`attach`, run under the engine lock."""
    alias = cache.get(key)
    if alias is None:
        conn = engine.connection()
        conn.execute("LOAD postgres")
        alias = f"__ovid2_pg_{uuid.uuid4().hex[:8]}"
        secret = f"{alias}_secret"
        # DuckDB's CREATE SECRET takes no bound parameters; values are
        # escaped literals and the secret is session-scoped (TEMPORARY).
        conn.execute(
            f"CREATE TEMPORARY SECRET {ident(secret)} ("
            f"TYPE postgres, HOST {_lit(kwargs['host'])}, PORT {int(kwargs['port'])}, "
            f"DATABASE {_lit(kwargs['dbname'])}, USER {_lit(kwargs['user'])}, "
            f"PASSWORD {_lit(str(kwargs.get('password') or ''))})"
        )
        conn.execute(f"ATTACH '' AS {ident(alias)} (TYPE postgres, SECRET {ident(secret)})")
        cache[key] = alias
    return alias, schema


def _lit(value: str) -> str:
    from ovid2.core.engines._java_datetime import sql_string

    return sql_string(str(value))


@register_duckdb_strategy
class DuckDBPostgresStrategy(DuckDBStagingStrategy):
    """postgres <-> DuckDB."""

    driver_type = "postgres"

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
        """Scan ``schema.table`` lazily, every column cast to VARCHAR."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        alias, schema = attach(engine, driver)
        table = file_name if file_name and "*" not in file_name else (table_name or file_name)
        conn = engine.connection()
        rel = conn.sql(f"SELECT * FROM {ident(alias)}.{ident(schema)}.{ident(table)}")
        items = ", ".join(f"CAST({ident(c)} AS VARCHAR) AS {ident(c)}" for c in rel.columns)
        return cast("DuckFrame", engine.wrap(rel.project(items)))

    def persist(
        self,
        df: DuckFrame,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """``CREATE OR REPLACE TABLE schema.table AS SELECT …`` into Postgres."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        alias, schema = attach(engine, driver)
        conn = engine.connection()
        view = f"__ovid2_pg_out_{uuid.uuid4().hex[:8]}"
        df.relation.create_view(view, replace=True)
        target = f"{ident(alias)}.{ident(schema)}.{ident(table_name.lower())}"
        try:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(alias)}.{ident(schema)}")
            conn.execute(f"DROP TABLE IF EXISTS {target}")
            conn.execute(f"CREATE TABLE {target} AS SELECT * FROM {ident(view)}")
            row = conn.sql(f"SELECT count(*) FROM {target}").fetchone()  # noqa: OVID2001 — written row count
            engine.add_rows_written(int(row[0]) if row else 0)
        finally:
            with contextlib.suppress(Exception):
                conn.execute(f"DROP VIEW IF EXISTS {ident(view)}")
