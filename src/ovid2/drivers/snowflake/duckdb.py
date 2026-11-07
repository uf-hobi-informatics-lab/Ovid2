"""DuckDB data plane for the snowflake driver.

* **Read** — run ``SELECT * FROM db.schema.TABLE`` through the Python
  connector and stream the result as Arrow batches into a scratch
  Parquet file, which DuckDB then scans lazily. Every column is cast to
  VARCHAR (the disc reader's ``inferSchema=false`` contract, identical
  to the Snowpark native strategy) so unions never coerce.
* **Write** — execute the DuckDB plan once into a directory of ~200 MB
  scratch Parquet parts (``PER_THREAD_OUTPUT`` + ``FILE_SIZE_BYTES``),
  ``PUT`` them in parallel to a temporary internal stage, ``CREATE OR REPLACE TABLE``
  with the CDM's declared Snowflake types (``VARCHAR(N)`` / ``DATE`` /
  ``NUMBER``, the same widths the Spark writer applies), and
  ``COPY INTO`` with ``MATCH_BY_COLUMN_NAME`` and ``ON_ERROR=CONTINUE``.
  VARCHAR values are truncated to the declared width and dates parsed
  leniently before the upload, mirroring the Spark writer.

Credentials come from :func:`ovid2.drivers.snowflake.probe.build_connect_kwargs`
(key-pair files or password secret) — the executor-tier path every
other Snowflake data-plane call uses.

ovid2: allow-sql-fstring — Snowflake statements interpolate identifiers
quoted by :func:`ovid2.drivers._layout.sql_ident` and engine-generated
stage / file names; all values are validated upstream.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import uuid
from typing import TYPE_CHECKING, Any, cast

from ovid2.core.engines._duck_column import ident
from ovid2.core.engines.duckdb_stager import (
    DuckDBStagingStrategy,
    local_scratch_dir,
    register_duckdb_strategy,
)

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _connect(driver: Driver, database: str, schema: str | None) -> Any:
    import snowflake.connector

    from ovid2.drivers.snowflake.probe import build_connect_kwargs

    kwargs = dict(build_connect_kwargs(dict(driver.args), dict(driver.secrets)))
    kwargs["database"] = database
    if schema:
        kwargs["schema"] = schema
    kwargs.setdefault("client_session_keep_alive", True)
    return snowflake.connector.connect(**kwargs)


def _db_schema(driver: Driver, *, write: bool) -> tuple[str, str]:
    from ovid2.drivers._layout import warehouse_db_schema

    database, schema = warehouse_db_schema(getattr(driver, "location", None))
    if not database:
        raise ValueError("snowflake address has no database")
    if not schema:
        if write:
            raise ValueError("snowflake output address has no schema")
        schema = "PUBLIC"
    return database, schema


def _dbtable(file_name: str, table_name: str | None) -> str:
    if file_name and _SQL_IDENTIFIER_RE.match(file_name):
        return file_name.upper()
    if table_name and _SQL_IDENTIFIER_RE.match(table_name):
        return table_name.upper()
    raise ValueError(
        f"snowflake read: file_name={file_name!r} is not a table identifier and "
        f"table_name={table_name!r} gives no fallback"
    )


@register_duckdb_strategy
class DuckDBSnowflakeStrategy(DuckDBStagingStrategy):
    """snowflake <-> DuckDB."""

    driver_type = "snowflake"

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
        """Pull ``db.schema.TABLE`` as Arrow into scratch Parquet (all VARCHAR)."""
        del pipeline, batch_id, delimiter
        import pyarrow as pa
        import pyarrow.parquet as pq

        from ovid2.drivers._layout import sql_ident

        database, schema = _db_schema(driver, write=False)
        table = _dbtable(file_name, table_name)
        fqtn = f"{sql_ident(database)}.{sql_ident(schema)}.{sql_ident(table)}"
        engine: Any = self._engine
        scratch = local_scratch_dir(engine)
        target = os.path.join(scratch, f"{table}.parquet")
        conn = _connect(driver, database, schema)
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {fqtn}")
            names = [d[0] for d in cur.description]
            schema_pa = pa.schema([pa.field(n, pa.string()) for n in names])
            writer = pq.ParquetWriter(target, schema_pa, compression="zstd")
            try:
                for batch in cur.fetch_arrow_batches():
                    cols = [batch.column(i).cast(pa.string()) for i in range(batch.num_columns)]
                    writer.write_table(pa.Table.from_arrays(cols, schema=schema_pa))
            finally:
                writer.close()
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
        """Upload via a temporary stage into a CDM-typed table (overwrite)."""
        del pipeline, batch_id, delimiter
        from ovid2.drivers._layout import sql_ident
        from ovid2.schema.cdm import build_snowflake_typed_columns, build_snowflake_write_schema

        database, schema = _db_schema(driver, write=True)
        table = table_name.upper()
        cdm_name = str((getattr(driver, "location", None) or {}).get("cdm") or "pcornet_7_0")
        spec = build_snowflake_write_schema(f"{cdm_name}.{table}")
        declared = dict(build_snowflake_typed_columns(f"{cdm_name}.{table}"))
        if not spec:
            logger.warning(
                "No CDM schema for %s.%s; writing every column as VARCHAR.", cdm_name, table
            )
        engine: Any = self._engine
        typed = _typed_projection(df, spec)
        scratch = local_scratch_dir(engine)
        # A *directory* of Parquet parts: Snowflake parallelises both PUT
        # and COPY INTO per file, so a single file loads on one thread
        # (an ACME ``encounter`` spent 5 min here vs 15 s computing).
        local = os.path.join(scratch, table)
        view = f"__ovid2_sf_{uuid.uuid4().hex[:8]}"
        typed.relation.create_view(view, replace=True)
        duck = engine.connection()
        try:
            rows = duck.execute(
                f"COPY (SELECT * FROM {ident(view)}) TO '{local}' "
                "(FORMAT parquet, COMPRESSION zstd, PER_THREAD_OUTPUT true, "
                f"FILE_SIZE_BYTES '{_PART_SIZE}', OVERWRITE true)"
            ).fetchone()  # noqa: OVID2001 — COPY's own row-count result, no re-scan
        finally:
            with contextlib.suppress(Exception):
                duck.execute(f"DROP VIEW IF EXISTS {ident(view)}")
        engine.add_rows_written(int(rows[0]) if rows else 0)
        parts = (
            sorted(n for n in os.listdir(local) if n.endswith(".parquet"))
            if os.path.isdir(local)
            else []
        )

        columns = list(typed.columns)
        ddl_types = [declared.get(c.upper(), "VARCHAR") for c in columns]
        fq_schema = f"{sql_ident(database)}.{sql_ident(schema)}"
        fqtn = f"{fq_schema}.{sql_ident(table)}"
        stage = f"OVID2_DUCK_{uuid.uuid4().hex[:10].upper()}"
        conn = _connect(driver, database, schema)
        try:
            cur = conn.cursor()
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {fq_schema}")
            cur.execute(f"USE SCHEMA {fq_schema}")
            cur.execute(f"CREATE TEMPORARY STAGE {stage} FILE_FORMAT = (TYPE = PARQUET)")
            if parts:
                cur.execute(
                    f"PUT 'file://{local}/*.parquet' @{stage} AUTO_COMPRESS = FALSE "
                    f"PARALLEL = {_put_parallel(engine)} OVERWRITE = TRUE"
                )
            column_ddl = ", ".join(
                f'"{c.upper()}" {t}' for c, t in zip(columns, ddl_types, strict=True)
            )
            cur.execute(f"CREATE OR REPLACE TABLE {fqtn} ({column_ddl})")
            if parts:
                cur.execute(
                    f"COPY INTO {fqtn} FROM @{stage} FILE_FORMAT = (TYPE = PARQUET) "
                    "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE ON_ERROR = CONTINUE PURGE = TRUE"
                )
            logger.info(
                "Snowflake write succeeded: %s.%s.%s (%s rows)",
                database,
                schema,
                table,
                rows[0] if rows else "?",
            )
        finally:
            with contextlib.suppress(Exception):
                conn.cursor().execute(f"DROP STAGE IF EXISTS {stage}")
            with contextlib.suppress(Exception):
                conn.close()
            shutil.rmtree(local, ignore_errors=True)


#: Target size of one uploaded Parquet part (Snowflake recommends
#: 100-250 MB compressed files for parallel ``COPY INTO``).
_PART_SIZE = "200MB"


def _put_parallel(engine: Any) -> int:
    """Return the ``PUT … PARALLEL`` degree (Snowflake caps it at 99)."""
    threads = 16
    with contextlib.suppress(Exception):
        row = engine.connection().execute("SELECT current_setting('threads')").fetchone()  # noqa: OVID2001 — config read
        threads = int(row[0]) if row else threads
    return max(1, min(99, threads))


def _typed_projection(df: DuckFrame, spec: list[tuple[str, str, int]]) -> DuckFrame:
    """Apply the CDM write schema (dates parsed, numbers cast, VARCHAR truncated)."""
    from ovid2.core.engines import _java_datetime as jd

    by_name = {name.upper(): (kind, width) for name, kind, width in spec}
    items: list[str] = []
    for idx, column in enumerate(df.columns, start=1):
        ref = f"#{idx}"
        kind, width = by_name.get(column.upper(), ("varchar", 0))
        if kind == "date":
            parsed = jd.parse_sql(f"CAST({ref} AS VARCHAR)", "yyyy-MM-dd")
            items.append(f"CAST({parsed} AS DATE) AS {ident(column.upper())}")
        elif kind == "number":
            items.append(
                f"TRY_CAST(trim(CAST({ref} AS VARCHAR)) AS DOUBLE) AS {ident(column.upper())}"
            )
        elif width:
            items.append(
                f"substring(CAST({ref} AS VARCHAR), 1, {int(width)}) AS {ident(column.upper())}"
            )
        else:
            items.append(f"CAST({ref} AS VARCHAR) AS {ident(column.upper())}")
    return df._derive(df.relation.project(", ".join(items)))
