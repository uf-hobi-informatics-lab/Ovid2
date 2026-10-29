"""Shared DB-API browsing + table introspection for SQL warehouses.

Postgres, SQL Server and Databricks expose the same three-level
namespace the Data Space uses for warehouses —
``DATABASE (catalog) / SCHEMA / TABLE`` — and the same introspection
needs: list the levels, describe a table (columns, row count / estimate,
size, last-modified, definition), sample it, stream it, count it.

A :class:`SqlDialect` captures the per-engine differences (identifier
quoting, catalogue queries, ``LIMIT`` vs ``TOP``, DDL source); the
functions here are dialect-agnostic and take a ``connect(database)``
callable returning a DB-API 2.0 connection. Metadata queries are
parameterised; object names that must appear in SQL text (``SELECT *
FROM <table>``) are quoted with :meth:`SqlDialect.quote`, which rejects
control characters and doubles the closing quote — never interpolated
raw. (``ovid2: allow-sql-fstring`` is *not* claimed: no f-string SQL.)

Import-safe: stdlib only; the vendor driver is imported by the caller.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ovid2.drivers.base import ColumnInfo, StorageEntry, TableInfo, TableSample

__all__ = [
    "SqlDialect",
    "count_rows",
    "describe_table",
    "iter_rows",
    "list_level",
    "sample_rows",
    "split_path",
]

Connect = Callable[[str | None], Any]


@dataclass(frozen=True)
class SqlDialect:
    """Per-engine SQL text for browsing + introspection.

    Every query string uses ``{p}`` as the bind-parameter placeholder
    token; :meth:`sql` swaps it for the driver's paramstyle marker.

    Attributes:
        name: Engine name (``postgres`` / ``mssql`` / ``databricks``).
        quote_open / quote_close: Identifier quote characters.
        paramstyle: ``format`` (``%s``) | ``qmark`` (``?``) | ``pyformat``.
        databases_sql: Lists databases / catalogs (no parameters).
        schemas_sql: Lists schemas of the connected database (no params).
        tables_sql: Lists ``(name, kind, rows, bytes, modified)`` of one
            schema — one ``{p}`` (schema).
        columns_sql: ``(name, type, nullable, char_len, precision, scale)``
            — two ``{p}`` (schema, table).
        hidden_schemas: Schemas never listed.
        hidden_databases: Databases never listed.
        top_syntax: ``True`` → ``SELECT TOP n *`` instead of ``LIMIT n``.
        ddl: Optional ``ddl(cursor, database, schema, table, columns)``
            → definition text.
        per_database_connect: ``True`` when a connection is bound to a
            single database (postgres) — schema / table listing then
            reconnects with ``connect(database)``.
        cross_database_names: ``True`` when names may be three-part
            (``db.schema.table``) on one connection (mssql, databricks).
    """

    name: str
    quote_open: str
    quote_close: str
    paramstyle: str
    databases_sql: str
    schemas_sql: str
    tables_sql: str
    columns_sql: str
    hidden_schemas: frozenset[str] = frozenset()
    hidden_databases: frozenset[str] = frozenset()
    top_syntax: bool = False
    ddl: Callable[..., str | None] | None = None
    per_database_connect: bool = True
    cross_database_names: bool = False
    schema_scoped_sql: Callable[[SqlDialect, str], str] | None = field(default=None)

    def sql(self, text: str) -> str:
        """Replace ``{p}`` tokens with the paramstyle marker."""
        marker = {"qmark": "?", "format": "%s", "pyformat": "%s", "named": "?"}.get(
            self.paramstyle, "%s"
        )
        return text.replace("{p}", marker)

    def execute(self, cur: Any, text: str, params: tuple[Any, ...] = ()) -> None:
        """Execute ``text`` with ``params`` bound in the dialect's paramstyle.

        ``named`` (Databricks SQL connector ≥ 3: ``:p0, :p1 …``) binds a
        dict; every other style binds the tuple positionally.
        """
        if self.paramstyle == "named":
            parts = text.split("{p}")
            built = parts[0]
            for i, rest in enumerate(parts[1:]):
                built += f":p{i}" + rest
            cur.execute(built, {f"p{i}": v for i, v in enumerate(params)})
            return
        if params:
            cur.execute(self.sql(text), params)
        else:
            cur.execute(self.sql(text))

    def quote(self, name: str) -> str:
        """Quote one identifier.

        Raises:
            ValueError: On an empty name or a control character.
        """
        if not name or any(ord(ch) < 32 for ch in name):
            raise ValueError(f"Refusing unsafe identifier {name!r}.")
        doubled = name.replace(self.quote_close, self.quote_close * 2)
        return self.quote_open + doubled + self.quote_close

    def qualified(self, *parts: str) -> str:
        """Return a dotted, quoted name."""
        return ".".join(self.quote(p) for p in parts if p)


def split_path(path: str | None) -> list[str]:
    """Split a ``DATABASE/SCHEMA/TABLE`` listing path (up to three segments).

    Raises:
        ValueError: On more than three segments.
    """
    parts = [p for p in (path or "").strip("/").split("/") if p]
    if len(parts) > 3:
        raise ValueError(f"Warehouse paths are DATABASE/SCHEMA/TABLE; got {path!r}.")
    return parts


def _iso(value: Any) -> str | None:
    if value is None or not hasattr(value, "isoformat"):
        return None
    from ovid2.drivers._tabular import iso_utc

    return iso_utc(value)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@contextlib.contextmanager
def _cursor(connect: Connect, database: str | None) -> Iterator[Any]:
    conn = connect(database)
    try:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            with contextlib.suppress(Exception):
                cur.close()
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def list_level(dialect: SqlDialect, connect: Connect, prefix: str | None) -> list[StorageEntry]:
    """List databases (root), schemas (``DB``) or tables (``DB/SCHEMA``)."""
    parts = split_path(prefix)
    if len(parts) == 3:
        raise ValueError("A table has no children; open it instead.")
    if not parts:
        with _cursor(connect, None) as cur:
            dialect.execute(cur, dialect.databases_sql)
            names = sorted({str(r[0]) for r in cur.fetchall() if r and r[0]}, key=str.lower)
        return [
            StorageEntry(name=n, kind="dir", path=n)
            for n in names
            if n.lower() not in dialect.hidden_databases
        ]
    database = parts[0]
    if len(parts) == 1:
        with _cursor(connect, database) as cur:
            dialect.execute(cur, _scoped(dialect, dialect.schemas_sql, database))
            names = sorted({str(r[0]) for r in cur.fetchall() if r and r[0]}, key=str.lower)
        return [
            StorageEntry(name=n, kind="dir", path=f"{database}/{n}")
            for n in names
            if n.lower() not in dialect.hidden_schemas
        ]
    schema = parts[1]
    with _cursor(connect, database) as cur:
        dialect.execute(cur, _scoped(dialect, dialect.tables_sql, database), (schema,))
        rows = cur.fetchall()
    out: list[StorageEntry] = []
    for r in rows:
        name = str(r[0])
        out.append(
            StorageEntry(
                name=name,
                kind="table",
                path=f"{database}/{schema}/{name}",
                row_count=_int(r[2]) if len(r) > 2 else None,
                size_bytes=_int(r[3]) if len(r) > 3 else None,
                modified_at=_iso(r[4]) if len(r) > 4 else None,
            )
        )
    out.sort(key=lambda e: e.name.lower())
    return out


def _scoped(dialect: SqlDialect, text: str, database: str) -> str:
    """Apply the dialect's database scoping (``[db].INFORMATION_SCHEMA``…)."""
    if dialect.schema_scoped_sql is None:
        return text
    return text.replace("{db}", dialect.schema_scoped_sql(dialect, database))


def _table_parts(path: str) -> tuple[str, str, str]:
    parts = split_path(path)
    if len(parts) != 3:
        raise ValueError(f"A table path is DATABASE/SCHEMA/TABLE; got {path!r}.")
    return parts[0], parts[1], parts[2]


def _type_label(data_type: Any, char_len: Any, precision: Any, scale: Any) -> str:
    t = str(data_type or "")
    lowered = t.lower()
    if char_len and int(char_len) > 0 and ("char" in lowered or lowered == "text"):
        return f"{t}({int(char_len)})"
    if precision is not None and lowered in ("numeric", "decimal", "number"):
        return f"{t}({int(precision)},{int(scale or 0)})"
    return t


def _table_name(dialect: SqlDialect, database: str, schema: str, table: str) -> str:
    if dialect.cross_database_names:
        return dialect.qualified(database, schema, table)
    return dialect.qualified(schema, table)


def describe_table(dialect: SqlDialect, connect: Connect, path: str) -> TableInfo:
    """Describe ``DATABASE/SCHEMA/TABLE`` (catalogue queries only).

    Raises:
        FileNotFoundError: When the table has no columns (absent / hidden).
    """
    database, schema, table = _table_parts(path)
    with _cursor(connect, database) as cur:
        dialect.execute(cur, _scoped(dialect, dialect.columns_sql, database), (schema, table))
        col_rows = cur.fetchall()
        if not col_rows:
            raise FileNotFoundError(f"Table {database}.{schema}.{table} does not exist.")
        columns = tuple(
            ColumnInfo(
                name=str(r[0]),
                type=_type_label(
                    r[1],
                    r[3] if len(r) > 3 else None,
                    r[4] if len(r) > 4 else None,
                    r[5] if len(r) > 5 else None,
                ),
                nullable=(str(r[2]).upper() in ("YES", "TRUE", "1")) if r[2] is not None else None,
            )
            for r in col_rows
        )
        dialect.execute(cur, _scoped(dialect, dialect.tables_sql, database), (schema,))
        meta = next((r for r in cur.fetchall() if str(r[0]) == table), None)
        kind = str(meta[1]).lower() if meta and len(meta) > 1 and meta[1] else "table"
        is_view = "view" in kind
        ddl: str | None = None
        if dialect.ddl is not None:
            with contextlib.suppress(Exception):
                ddl = dialect.ddl(cur, dialect, database, schema, table, columns, is_view)
    rows = _int(meta[2]) if meta and len(meta) > 2 else None
    return TableInfo(
        name=table,
        path=path,
        kind="view" if is_view else "table",
        format="view" if is_view else "table",
        columns=columns,
        row_count=rows,
        row_count_exact=rows is None,
        size_bytes=_int(meta[3]) if meta and len(meta) > 3 else None,
        modified_at=_iso(meta[4]) if meta and len(meta) > 4 else None,
        ddl=ddl,
        message=(
            "Row count is the engine's statistics estimate; use Count rows for an exact one."
            if rows is not None
            else None
        ),
    )


def iter_rows(
    dialect: SqlDialect,
    connect: Connect,
    path: str,
    limit: int | None = None,
    *,
    batch_size: int = 5000,
) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
    """Stream ``SELECT *`` (``LIMIT`` / ``TOP``-ed when ``limit`` is set)."""
    database, schema, table = _table_parts(path)
    name = _table_name(dialect, database, schema, table)
    if limit is None:
        text = "SELECT * FROM " + name
    elif dialect.top_syntax:
        text = "SELECT TOP " + str(int(limit)) + " * FROM " + name
    else:
        text = "SELECT * FROM " + name + " LIMIT " + str(int(limit))
    conn = connect(database)
    cur = conn.cursor()
    try:
        cur.execute(text)
    except Exception:
        with contextlib.suppress(Exception):
            cur.close()
        with contextlib.suppress(Exception):
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
            with contextlib.suppress(Exception):
                cur.close()
            with contextlib.suppress(Exception):
                conn.close()

    return columns, _batches()


def sample_rows(dialect: SqlDialect, connect: Connect, path: str, limit: int = 100) -> TableSample:
    """Return the first ``limit`` (≤1000) rows."""
    from ovid2.drivers._tabular import MAX_SAMPLE_ROWS

    limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    columns, batches = iter_rows(dialect, connect, path, limit + 1, batch_size=limit + 1)
    rows: list[tuple[Any, ...]] = []
    for batch in batches:
        rows.extend(batch)
    return TableSample(
        columns=tuple(columns), rows=tuple(rows[:limit]), truncated=len(rows) > limit
    )


def count_rows(dialect: SqlDialect, connect: Connect, path: str) -> int:
    """Exact ``COUNT(*)``."""
    database, schema, table = _table_parts(path)
    with _cursor(connect, database) as cur:
        cur.execute("SELECT COUNT(*) FROM " + _table_name(dialect, database, schema, table))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def reconstruct_ddl(
    dialect: SqlDialect,
    schema: str,
    table: str,
    columns: tuple[ColumnInfo, ...],
    *,
    is_view: bool = False,
) -> str:
    """Rebuild a display-only ``CREATE TABLE`` from catalogue columns."""
    head = ("-- view; column list from the catalogue\n" if is_view else "") + "CREATE TABLE "
    body = ",\n".join(
        "  "
        + dialect.quote(c.name)
        + " "
        + (c.type or "")
        + (" NOT NULL" if c.nullable is False else "")
        for c in columns
    )
    return head + dialect.qualified(schema, table) + " (\n" + body + "\n);"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
