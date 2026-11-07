"""DuckDB data plane for the databricks driver (databricks-sql-connector).

Reads stream ``SELECT *`` through the SQL warehouse as Arrow
(``fetchmany_arrow``) into scratch Parquet (all VARCHAR). Writes
replace the target with ``STRING`` columns via parameterised
``executemany`` batches — adequate for the moderate table sizes an
ovid2 Databricks *destination* receives; very large loads should stage
through cloud storage instead.

ovid2: allow-sql-fstring — identifiers are backtick-quoted with
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
    return "`" + str(name).replace("`", "``") + "`"


def _location(driver: Driver) -> tuple[str, str]:
    from ovid2.drivers._layout import warehouse_db_schema

    catalog, schema = warehouse_db_schema(getattr(driver, "location", None))
    catalog = catalog or str(driver.args.get("catalog") or "")
    if not catalog or not schema:
        raise ValueError("databricks address needs a catalog and a schema")
    return catalog, schema


@register_duckdb_strategy
class DuckDBDatabricksStrategy(DuckDBStagingStrategy):
    """databricks <-> DuckDB."""

    driver_type = "databricks"

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
        """Spool ``SELECT * FROM catalog.schema.table`` into scratch Parquet."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        catalog, schema = _location(driver)
        table = file_name if file_name and "*" not in file_name else (table_name or file_name)
        target = os.path.join(local_scratch_dir(engine), f"{table}.parquet")
        conn = driver._connect(catalog)  # type: ignore[attr-defined]
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {_quote(catalog)}.{_quote(schema)}.{_quote(table)}")
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
        """Replace ``catalog.schema.table`` (STRING columns)."""
        del pipeline, batch_id, delimiter
        engine: Any = self._engine
        catalog, schema = _location(driver)
        local = os.path.join(
            local_scratch_dir(engine), f"{table_name}-{uuid.uuid4().hex[:6]}.parquet"
        )
        view = f"__ovid2_dbx_{uuid.uuid4().hex[:8]}"
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
        target = f"{_quote(catalog)}.{_quote(schema)}.{_quote(table_name.lower())}"
        create = (
            f"CREATE TABLE {target} (" + ", ".join(f"{_quote(c)} STRING" for c in df.columns) + ")"
        )
        conn = driver._connect(catalog)  # type: ignore[attr-defined]
        try:
            conn.cursor().execute(f"CREATE SCHEMA IF NOT EXISTS {_quote(catalog)}.{_quote(schema)}")
            engine.add_rows_written(
                write_rows(
                    conn,
                    parquet_path=local,
                    target_sql=target,
                    columns=list(df.columns),
                    quote=_quote,
                    placeholder="?",
                    create_sql=create,
                )
            )
        finally:
            with contextlib.suppress(Exception):
                conn.close()
