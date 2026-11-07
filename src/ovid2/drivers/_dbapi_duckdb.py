"""Shared DB-API <-> DuckDB transfer used by the warehouse / Access strategies.

Drivers without a native DuckDB scanner (``mssql``, ``databricks``,
``access``) read through their Python DB-API connector: rows are
fetched in batches, every value rendered to text (the disc reader's
all-VARCHAR contract), and spooled into a scratch Parquet file that
DuckDB scans lazily. Writes go the other way: the plan executes once
into scratch Parquet, which is streamed back as parameterised
``executemany`` batches.

ovid2: allow-sql-fstring — statements interpolate only identifiers
quoted by the caller's dialect quoting function.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

__all__ = ["rows_to_parquet", "text_value", "write_rows"]


def text_value(value: Any) -> str | None:
    """Render one DB-API value as text (the Spark CSV-read contract)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    return str(value)


def rows_to_parquet(
    columns: Sequence[str],
    batches: Iterable[Sequence[Sequence[Any]]],
    target: str,
) -> int:
    """Write text-rendered ``batches`` to ``target`` (Parquet). Returns row count."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([pa.field(str(c), pa.string()) for c in columns])
    total = 0
    writer = pq.ParquetWriter(target, schema, compression="zstd")
    try:
        for batch in batches:
            if not batch:
                continue
            cols = list(zip(*batch, strict=True)) if batch else [()] * len(columns)
            arrays = [pa.array([text_value(v) for v in col], type=pa.string()) for col in cols]
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            total += len(batch)
    finally:
        writer.close()
    return total


def cursor_batches(cursor: Any, batch_size: int = 50_000) -> Iterator[list[Any]]:
    """Yield ``fetchmany`` batches until the cursor is exhausted."""
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield list(rows)


def write_rows(
    connection: Any,
    *,
    parquet_path: str,
    target_sql: str,
    columns: Sequence[str],
    quote: Callable[[str], str],
    placeholder: str,
    create_sql: str | None,
    batch_size: int = 5_000,
) -> int:
    """Replace ``target_sql`` with the rows of ``parquet_path`` (all VARCHAR).

    Args:
        connection: Open DB-API connection.
        parquet_path: Executed plan (scratch Parquet).
        target_sql: Fully-qualified, quoted target table.
        columns: Column names, in order.
        quote: Identifier quoting function for the target dialect.
        placeholder: DB-API placeholder (``%s`` / ``?``).
        create_sql: ``CREATE TABLE`` statement (run after a drop), or
            ``None`` to keep the existing table and only delete its rows.
        batch_size: Rows per ``executemany``.

    Returns:
        Number of rows inserted.
    """
    import pyarrow.parquet as pq

    cursor = connection.cursor()
    try:
        if create_sql is not None:
            cursor.execute(f"DROP TABLE IF EXISTS {target_sql}")
            cursor.execute(create_sql)
        else:
            cursor.execute(f"DELETE FROM {target_sql}")
        column_sql = ", ".join(quote(c) for c in columns)
        values_sql = ", ".join(placeholder for _ in columns)
        insert = f"INSERT INTO {target_sql} ({column_sql}) VALUES ({values_sql})"
        total = 0
        parquet = pq.ParquetFile(parquet_path)
        for batch in parquet.iter_batches(batch_size=batch_size):
            rows = list(zip(*[col.to_pylist() for col in batch.columns], strict=True))
            if rows:
                cursor.executemany(insert, rows)
                total += len(rows)
        connection.commit()
        return total
    finally:
        cursor.close()
        if os.path.exists(parquet_path):
            os.unlink(parquet_path)
