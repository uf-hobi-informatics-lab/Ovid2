"""Snowflake table introspection for the Data Space explorer.

Implements the :class:`~ovid2.drivers.base.Driver` table hooks
(``describe_table`` / ``sample_rows`` / ``iter_rows`` / ``count_rows``)
for a ``DATABASE/SCHEMA/TABLE`` listing path using metadata queries
only (``INFORMATION_SCHEMA``, ``GET_DDL``) plus a bounded ``LIMIT``
for samples. Every statement is parameterised: object names are bound
through ``IDENTIFIER(%s)`` with a fully double-quoted name, never
interpolated into SQL text.

``snowflake-connector-python`` is imported lazily (import-safe module).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from ovid2.drivers.base import ColumnInfo, TableInfo, TableSample

__all__ = [
    "count_rows_from_config",
    "describe_table_from_config",
    "iter_rows_from_config",
    "quoted_name",
    "sample_rows_from_config",
    "split_table_path",
]

_TABLE_META_SQL = (
    "SELECT TABLE_TYPE, ROW_COUNT, BYTES, CREATED, LAST_ALTERED, COMMENT "
    "FROM IDENTIFIER(%s) WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
    "NUMERIC_PRECISION, NUMERIC_SCALE "
    "FROM IDENTIFIER(%s) WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY ORDINAL_POSITION"
)
_DDL_SQL = "SELECT GET_DDL(%s, %s)"
_SAMPLE_SQL = "SELECT * FROM IDENTIFIER(%s) LIMIT %s"
_ALL_SQL = "SELECT * FROM IDENTIFIER(%s)"
_COUNT_SQL = "SELECT COUNT(*) FROM IDENTIFIER(%s)"


def split_table_path(path: str) -> tuple[str, str, str]:
    """Split ``DATABASE/SCHEMA/TABLE`` into its parts.

    Raises:
        ValueError: When ``path`` is not exactly three segments.
    """
    parts = [p for p in (path or "").strip("/").split("/") if p]
    if len(parts) != 3:
        raise ValueError(f"A Snowflake table path is DATABASE/SCHEMA/TABLE; got {path!r}.")
    return parts[0], parts[1], parts[2]


def _q(name: str) -> str:
    """Double-quote one identifier (embedded quotes doubled)."""
    return '"' + name.replace('"', '""') + '"'


def quoted_name(*parts: str) -> str:
    """Return a fully-qualified, double-quoted object name."""
    return ".".join(_q(p) for p in parts)


def _connect(args: dict[str, Any], secrets: dict[str, Any]) -> Any:
    import snowflake.connector as sf

    from ovid2.drivers.snowflake.probe import build_connect_kwargs

    return sf.connect(**build_connect_kwargs(args, secrets))


def _iso(value: Any) -> str | None:
    from ovid2.drivers._tabular import iso_utc

    return iso_utc(value) if value is not None else None


def _type_label(data_type: str, char_len: Any, precision: Any, scale: Any) -> str:
    """Render ``DATA_TYPE`` with its length / precision."""
    t = str(data_type or "").upper()
    if t in ("TEXT", "VARCHAR", "STRING", "CHAR", "CHARACTER") and char_len:
        return f"VARCHAR({int(char_len)})"
    if t in ("NUMBER", "DECIMAL", "NUMERIC") and precision is not None:
        return f"NUMBER({int(precision)},{int(scale or 0)})"
    return t


def describe_table_from_config(
    args: dict[str, Any], secrets: dict[str, Any], path: str
) -> TableInfo:
    """Return the :class:`TableInfo` for ``DATABASE/SCHEMA/TABLE``.

    Raises:
        FileNotFoundError: When the table does not exist (or is not visible).
    """
    database, schema, table = split_table_path(path)
    conn = _connect(args, secrets)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                _TABLE_META_SQL,
                (quoted_name(database, "INFORMATION_SCHEMA", "TABLES"), schema, table),
            )
            meta = cur.fetchone()
            if meta is None:
                raise FileNotFoundError(f"Table {database}.{schema}.{table} does not exist.")
            table_type, row_count, size, created, altered, comment = meta
            is_view = "VIEW" in str(table_type or "").upper()
            cur.execute(
                _COLUMNS_SQL,
                (quoted_name(database, "INFORMATION_SCHEMA", "COLUMNS"), schema, table),
            )
            columns = tuple(
                ColumnInfo(
                    name=str(r[0]),
                    type=_type_label(r[1], r[3], r[4], r[5]),
                    nullable=str(r[2] or "").upper() == "YES",
                )
                for r in cur.fetchall()
            )
            ddl: str | None = None
            with contextlib.suppress(Exception):
                cur.execute(
                    _DDL_SQL, ("VIEW" if is_view else "TABLE", quoted_name(database, schema, table))
                )
                row = cur.fetchone()
                ddl = str(row[0]) if row and row[0] else None
        finally:
            cur.close()
    finally:
        conn.close()
    return TableInfo(
        name=table,
        path=path,
        kind="view" if is_view else "table",
        format="view" if is_view else "table",
        columns=columns,
        row_count=int(row_count) if row_count is not None else None,
        row_count_exact=row_count is not None,
        size_bytes=int(size) if size is not None else None,
        modified_at=_iso(altered),
        created_at=_iso(created),
        ddl=ddl,
        message=str(comment) if comment else None,
    )


def iter_rows_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    path: str,
    limit: int | None = None,
    *,
    batch_size: int = 5000,
) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
    """Stream ``SELECT *`` (optionally ``LIMIT``-ed) in ``fetchmany`` batches.

    The connection stays open until the returned iterator is exhausted
    or closed.
    """
    database, schema, table = split_table_path(path)
    conn = _connect(args, secrets)
    cur = conn.cursor()
    name = quoted_name(database, schema, table)
    try:
        if limit is None:
            cur.execute(_ALL_SQL, (name,))
        else:
            cur.execute(_SAMPLE_SQL, (name, int(limit)))
    except Exception:
        cur.close()
        conn.close()
        raise
    columns = [str(d[0]) for d in (cur.description or [])]

    def _batches() -> Iterator[list[tuple[Any, ...]]]:
        try:
            while True:
                rows = cur.fetchmany(max(1, batch_size))
                if not rows:
                    return
                yield [tuple(r) for r in rows]
        finally:
            cur.close()
            conn.close()

    return columns, _batches()


def sample_rows_from_config(
    args: dict[str, Any], secrets: dict[str, Any], path: str, limit: int = 100
) -> TableSample:
    """Return the first ``limit`` rows (``LIMIT limit+1`` to detect truncation)."""
    from ovid2.drivers._tabular import MAX_SAMPLE_ROWS

    limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    columns, batches = iter_rows_from_config(args, secrets, path, limit + 1, batch_size=limit + 1)
    rows: list[tuple[Any, ...]] = []
    for batch in batches:
        rows.extend(batch)
    return TableSample(
        columns=tuple(columns), rows=tuple(rows[:limit]), truncated=len(rows) > limit
    )


def count_rows_from_config(args: dict[str, Any], secrets: dict[str, Any], path: str) -> int:
    """Return ``SELECT COUNT(*)`` for the table."""
    database, schema, table = split_table_path(path)
    conn = _connect(args, secrets)
    try:
        cur = conn.cursor()
        try:
            cur.execute(_COUNT_SQL, (quoted_name(database, schema, table),))
            row = cur.fetchone()
        finally:
            cur.close()
    finally:
        conn.close()
    return int(row[0]) if row else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
