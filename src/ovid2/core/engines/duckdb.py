"""DuckDB compute engine (in-process, vectorised, single node).

``DuckDBEngine`` runs the Ovid2 pipeline on an embedded DuckDB
connection: no JVM, no cluster, no network hop. It is the fastest engine
for data that fits a single host (DuckDB spills to disk past
``memory_limit``), and the only one that starts in milliseconds.

* **Session** — :class:`DuckDBEngineSession` (``table`` / ``sql`` /
  ``createDataFrame`` / ``read``) over one ``duckdb`` connection.
* **DataFrames** — :class:`~._duck_frame.DuckFrame`, a lazy
  PySpark-shaped wrapper so shared formatter / mapper / fixer code runs
  unchanged. Nothing executes until the stager writes.
* **Functions** — :class:`_DuckFunctions` implements
  :class:`~ovid2.core.engines.protocols.EngineFunctions` with Spark
  (LEGACY parser, ANSI off) semantics: ``to_date`` / ``to_timestamp``
  via :mod:`._java_datetime`, lenient casts, ``RLIKE`` search, Java
  regex replacement syntax.
* **Family** — ``duckdb``; ``session_is_spark = False`` so the pipeline
  takes the staged path (reads / writes through
  :class:`~ovid2.core.engines.duckdb_stager.DuckDBEngineStager`).

Only this module, :mod:`.duckdb_stager` and ``drivers/<type>/duckdb.py``
may ``import duckdb`` (``tests/contract/test_no_duckdb_outside_engine.py``).

ovid2: allow-sql-fstring — session-level ``SET``/``CREATE TEMP TABLE``
statements interpolate only engine-generated identifiers (quoted via
:func:`~._duck_column.ident`) and validated numeric settings.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ovid2.core.engines import _java_datetime as jd
from ovid2.core.engines._duck_column import DuckColumn, ident, literal_sql
from ovid2.core.engines._duck_expr import java_replacement_to_re2, spark_cast_sql, transpile
from ovid2.core.engines._duck_frame import DuckFrame, DuckWindow
from ovid2.core.engines.base import BaseEngine, EngineTestResult
from ovid2.core.engines.protocols import (
    EngineFamily,
    EngineFunctions,
    EngineOperationUnsupported,
    EngineSession,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ovid2.core.engines.metrics import EngineMetricsSample

_log = logging.getLogger(__name__)

#: Portable type name -> DuckDB type (``create_typed_dataframe``).
_PORTABLE_DUCK_TYPES: dict[str, str] = {
    "string": "VARCHAR",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "long": "BIGINT",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}

#: Spark ``xxhash64 & 0x7fff…`` analogue: DuckDB ``hash`` is UBIGINT.
_SIGN_MASK = "9223372036854775807"


def _as_varchar(c: DuckColumn) -> DuckColumn:
    return DuckColumn.template(lambda a: f"CAST({a} AS VARCHAR)", c, kind="str")


def _literal_value(value: Any) -> Any:
    """Return the Python value behind ``lit(x)`` / a bare literal.

    Raises:
        EngineOperationUnsupported: For a non-literal column (DuckDB
            ``create_map`` supports literal keys / values only).
    """
    if isinstance(value, DuckColumn):
        held = value.literal_value
        if held is None:
            raise EngineOperationUnsupported(
                "duckdb", "create_map", "only literal keys / values are supported"
            )
        return held[0]
    return value


#: ``create_map`` literals with more entries than this are backed by a
#: registered Arrow lookup table (hash-joined scalar subquery) instead of
#: an inline ``CASE``: DuckDB re-evaluates a large ``MAP {…}`` / ``CASE``
#: literal per vector, which is ~50x slower on a 25k-entry dictionary.
_MAP_TABLE_THRESHOLD = 64


class DuckMapLiteral:
    """A Spark ``create_map(k1, v1, …)`` literal, usable only via ``[key]``.

    Ovid2 code builds map literals solely to look values up
    (``create_map(...)[col("x")]``); that indexing is all this class
    supports. Missing keys (and a NULL key) yield NULL, as in Spark.
    """

    __slots__ = ("_engine", "_pairs")

    def __init__(self, pairs: list[tuple[Any, Any]], engine: Any | None) -> None:
        self._pairs = pairs
        self._engine = engine

    def __getitem__(self, key: Any) -> DuckColumn:
        """Return the value mapped to ``key`` (NULL when absent)."""
        from ovid2.core.engines._duck_column import to_column

        key_col = to_column(key, literal_strings=True)
        pairs = [(k, v) for k, v in self._pairs if k is not None]
        value_kinds = {literal_sql(v)[1] for _k, v in pairs} - {"null"}
        stringify = "str" in value_kinds and len(value_kinds) > 1
        kind = (
            "str"
            if "str" in value_kinds
            else (value_kinds.pop() if len(value_kinds) == 1 else "any")
        )
        if not pairs:
            return DuckColumn.raw("CAST(NULL AS VARCHAR)", "str")
        if self._engine is not None and len(pairs) > _MAP_TABLE_THRESHOLD:
            table = self._engine.register_lookup_table(pairs, stringify=stringify)

            def _render_table(ctx: Any) -> str:
                k = key_col.sql(ctx)
                if key_col.kind(ctx) not in ("str", "any"):
                    k = f"CAST({k} AS VARCHAR)"
                return f"(SELECT __v FROM {ident(table)} WHERE __k = {k})"

            return DuckColumn(_render_table, kind)

        rendered = [(literal_sql(str(k))[0], literal_sql(v)) for k, v in pairs]

        def _value(sql: str, vkind: str) -> str:
            return f"CAST({sql} AS VARCHAR)" if stringify and vkind not in ("str", "null") else sql

        def _render_case(ctx: Any) -> str:
            k = key_col.sql(ctx)
            if key_col.kind(ctx) not in ("str", "any"):
                k = f"CAST({k} AS VARCHAR)"
            whens = " ".join(f"WHEN {ks} THEN {_value(vs, vk)}" for ks, (vs, vk) in rendered)
            return f"(CASE {k} {whens} END)"

        return DuckColumn(_render_case, kind)

    getItem = __getitem__  # noqa: N815 — PySpark API


class _DuckFunctions:
    """:class:`EngineFunctions` over DuckDB SQL (Spark semantics)."""

    def __init__(self, engine: Any | None = None) -> None:
        #: Owning engine (``None`` when built by the DSL without one);
        #: used to back large ``create_map`` literals with a lookup table.
        self._engine = engine

    # ---- core ----------------------------------------------------------

    def col(self, name: str) -> DuckColumn:
        from ovid2.core.engines._duck_column import column_from_name

        return column_from_name(name)

    column = col

    def lit(self, value: Any) -> DuckColumn:
        if isinstance(value, DuckColumn):
            return value
        return DuckColumn.literal(value)

    def when(self, cond: Any, value: Any) -> DuckColumn:
        from ovid2.core.engines._duck_column import to_column

        return DuckColumn(
            lambda _c: "",
            case=([(to_column(cond), to_column(value, literal_strings=True))], None),
        )

    def coalesce(self, *cols: Any) -> DuckColumn:
        from ovid2.core.engines._duck_column import to_column

        columns = [to_column(c) for c in cols]

        def _render(ctx: Any) -> str:
            kinds = {c.kind(ctx) for c in columns} - {"null"}
            stringify = "str" in kinds and len(kinds) > 1
            parts = []
            for c in columns:
                sql = c.sql(ctx)
                if c._null_literal:
                    sql = "NULL"
                elif stringify and c.kind(ctx) != "str":
                    sql = f"CAST({sql} AS VARCHAR)"
                parts.append(sql)
            return f"COALESCE({', '.join(parts)})"

        def _kind(ctx: Any) -> str:
            kinds = {c.kind(ctx) for c in columns} - {"null"}
            if "str" in kinds:
                return "str"
            return kinds.pop() if len(kinds) == 1 else "any"

        return DuckColumn(_render, _kind)

    def upper(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"upper({a})", self._c(c), kind="str")

    def lower(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"lower({a})", self._c(c), kind="str")

    def initcap(self, c: Any) -> DuckColumn:
        # Spark: lower-case everything, upper-case the first letter of
        # each space-separated word.
        return DuckColumn.template(
            lambda a: (
                "array_to_string(list_transform(string_split(lower(CAST("
                + a
                + " AS VARCHAR)), ' '), lambda __w: upper(__w[1:1]) || __w[2:]), ' ')"
            ),
            self._c(c),
            kind="str",
        )

    def trim(self, c: Any) -> DuckColumn:
        # Spark trims ASCII spaces only, as DuckDB ``trim(x)`` does.
        return DuckColumn.template(lambda a: f"trim(CAST({a} AS VARCHAR))", self._c(c), kind="str")

    def ltrim(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"ltrim(CAST({a} AS VARCHAR))", self._c(c), kind="str")

    def rtrim(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"rtrim(CAST({a} AS VARCHAR))", self._c(c), kind="str")

    def substring(self, c: Any, pos: int, length: int) -> DuckColumn:
        start = 1 if int(pos) == 0 else int(pos)
        return DuckColumn.template(
            lambda a: f"substring(CAST({a} AS VARCHAR), {start}, {int(length)})",
            self._c(c),
            kind="str",
        )

    def regexp_replace(self, c: Any, pattern: str, replacement: str) -> DuckColumn:
        pat = jd.sql_string(pattern)
        rep = jd.sql_string(java_replacement_to_re2(replacement))
        return DuckColumn.template(
            lambda a: f"regexp_replace(CAST({a} AS VARCHAR), {pat}, {rep}, 'g')",
            self._c(c),
            kind="str",
        )

    def regexp_extract(self, c: Any, pattern: str, idx: int) -> DuckColumn:
        pat = jd.sql_string(pattern)
        # Spark returns '' on no match (NULL on NULL input), as DuckDB does.
        return DuckColumn.template(
            lambda a: f"regexp_extract(CAST({a} AS VARCHAR), {pat}, {int(idx)})",
            self._c(c),
            kind="str",
        )

    def split(self, c: Any, pattern: str, limit: int = -1) -> DuckColumn:
        del limit
        pat = jd.sql_string(pattern)
        return DuckColumn.template(
            lambda a: f"regexp_split_to_array(CAST({a} AS VARCHAR), {pat})", self._c(c)
        )

    def explode(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"unnest({a})", self._c(c))

    def array(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        return DuckColumn(lambda ctx: "[" + ", ".join(x.sql(ctx) for x in columns) + "]")

    def to_date(self, c: Any, fmt: str | None = None) -> DuckColumn:
        col = self._c(c)

        def _render(ctx: Any) -> str:
            value = col.sql(ctx)
            kind = col.kind(ctx)
            if kind in ("date", "ts") and fmt is None:
                return f"CAST({value} AS DATE)"
            text = value if kind == "str" else f"CAST({value} AS VARCHAR)"
            if fmt is None:
                return f"CAST({jd.cast_sql(text, timestamp=False)} AS DATE)"
            return f"CAST({jd.parse_sql(text, fmt)} AS DATE)"

        return DuckColumn(_render, "date")

    def to_timestamp(self, c: Any, fmt: str | None = None) -> DuckColumn:
        col = self._c(c)

        def _render(ctx: Any) -> str:
            value = col.sql(ctx)
            kind = col.kind(ctx)
            if kind in ("date", "ts") and fmt is None:
                return f"CAST({value} AS TIMESTAMP)"
            text = value if kind == "str" else f"CAST({value} AS VARCHAR)"
            if fmt is None:
                return jd.cast_sql(text, timestamp=True)
            return jd.parse_sql(text, fmt)

        return DuckColumn(_render, "ts")

    def date_format(self, c: Any, fmt: str) -> DuckColumn:
        col = self._c(c)
        pattern = jd.sql_string(jd.strftime_format(fmt))

        def _render(ctx: Any) -> str:
            value = col.sql(ctx)
            kind = col.kind(ctx)
            if kind in ("date", "ts"):
                ts = f"CAST({value} AS TIMESTAMP)"
            else:
                ts = jd.cast_sql(f"CAST({value} AS VARCHAR)", timestamp=True)
            return f"strftime({ts}, {pattern})"

        return DuckColumn(_render, "str")

    def datediff(self, end: Any, start: Any) -> DuckColumn:
        e, s = self.to_date(end), self.to_date(start)
        return DuckColumn.template(lambda a, b: f"date_diff('day', {b}, {a})", e, s, kind="num")

    def date_add(self, c: Any, days: int) -> DuckColumn:
        d = self.to_date(c)
        return DuckColumn.template(
            lambda a: f"CAST({a} + to_days({int(days)}) AS DATE)", d, kind="date"
        )

    def current_timestamp(self) -> DuckColumn:
        return DuckColumn.raw("current_localtimestamp()", "ts")

    def current_date(self) -> DuckColumn:
        return DuckColumn.raw("CAST(current_localtimestamp() AS DATE)", "date")

    def count(self, c: Any) -> DuckColumn:
        if isinstance(c, str) and c == "*":
            return DuckColumn.raw("count(*)", "num", name="count(1)")
        return DuckColumn.template(lambda a: f"count({a})", self._c(c), kind="num")

    def countDistinct(self, *cols: Any) -> DuckColumn:  # noqa: N802 — PySpark API
        columns = [self._c(c) for c in cols]
        return DuckColumn(
            lambda ctx: "count(DISTINCT " + ", ".join(x.sql(ctx) for x in columns) + ")", "num"
        )

    def max(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"max({col.sql(ctx)})", col.kind)

    def min(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"min({col.sql(ctx)})", col.kind)

    def sum(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(
            lambda ctx: (
                f"sum({'TRY_CAST(' + col.sql(ctx) + ' AS DOUBLE)' if col.kind(ctx) == 'str' else col.sql(ctx)})"
            ),
            "num",
        )

    def avg(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(
            lambda ctx: (
                f"avg({'TRY_CAST(' + col.sql(ctx) + ' AS DOUBLE)' if col.kind(ctx) == 'str' else col.sql(ctx)})"
            ),
            "num",
        )

    def first(self, c: Any, ignorenulls: bool = False) -> DuckColumn:
        col = self._c(c)
        fn = "first" if not ignorenulls else "any_value"
        return DuckColumn(lambda ctx: f"{fn}({col.sql(ctx)})", col.kind)

    def round(self, c: Any, scale: int = 0) -> DuckColumn:
        col = self._c(c)
        # Spark rounds HALF_UP; DuckDB ``round`` on DOUBLE is half away
        # from zero as well.
        return DuckColumn(
            lambda ctx: (
                f"round({'TRY_CAST(' + col.sql(ctx) + ' AS DOUBLE)' if col.kind(ctx) == 'str' else col.sql(ctx)}, {int(scale)})"
            ),
            "num",
        )

    def abs(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"abs({a})", self._c(c), kind="num")

    def broadcast(self, df: Any) -> Any:
        return df  # DuckDB plans joins itself.

    def input_file_name(self) -> DuckColumn:
        # Populated by the disc reader (``filename=true``) as a hidden
        # ``__ovid2_filename`` column when the stager is asked for it.
        return DuckColumn.ref(None, "__ovid2_filename")

    def expr(self, sql_string: str) -> DuckColumn:
        name = _expr_output_name(sql_string)
        # A trailing ``AS alias`` becomes the column's name, not SQL text
        # (the select list adds its own ``AS``).
        text = re.sub(
            r"\s+AS\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s*$", "", sql_string, flags=re.IGNORECASE
        )

        def _render(ctx: Any) -> str:
            from ovid2.core.engines._duck_frame import _SqlStringResolver

            return transpile(_SqlStringResolver(ctx).rewrite(text))

        return DuckColumn(_render, name=name)

    def sha2(self, c: Any, bits: int) -> DuckColumn:
        if int(bits) not in (0, 256):
            raise EngineOperationUnsupported("duckdb", f"sha2({bits})", "only SHA-256 is available")
        return DuckColumn.template(
            lambda a: f"sha256(CAST({a} AS VARCHAR))", self._c(c), kind="str"
        )

    def md5(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"md5(CAST({a} AS VARCHAR))", self._c(c), kind="str")

    # ---- portable extensions -------------------------------------------

    def concat(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        # Spark ``concat``: NULL if any input is NULL (DuckDB ``||``).
        return DuckColumn(
            lambda ctx: "(" + " || ".join(f"CAST({x.sql(ctx)} AS VARCHAR)" for x in columns) + ")",
            "str",
        )

    def concat_ws(self, sep: str, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        s = jd.sql_string(sep)
        return DuckColumn(
            lambda ctx: (
                f"concat_ws({s}, "
                + ", ".join(f"CAST({x.sql(ctx)} AS VARCHAR)" for x in columns)
                + ")"
            ),
            "str",
        )

    def map_from_dict(
        self, mapping: dict[str, Any], key_col: Any, default: Any = None
    ) -> DuckColumn:
        key = self._c(key_col)
        if not mapping:
            return self.lit(default)
        pairs = [(literal_sql(k)[0], literal_sql(v)) for k, v in mapping.items()]
        kinds = {kind for _k, (_s, kind) in pairs} - {"null"}
        default_sql, default_kind = literal_sql(default)
        if default_kind != "null":
            kinds.add(default_kind)
        stringify = "str" in kinds and len(kinds) > 1

        def _value(sql: str, kind: str) -> str:
            return f"CAST({sql} AS VARCHAR)" if stringify and kind not in ("str", "null") else sql

        def _render(ctx: Any) -> str:
            k = key.sql(ctx)
            if key.kind(ctx) not in ("str", "any"):
                k = f"CAST({k} AS VARCHAR)"
            whens = " ".join(
                f"WHEN {ksql} THEN {_value(vsql, vkind)}" for ksql, (vsql, vkind) in pairs
            )
            return f"(CASE {k} {whens} ELSE {_value(default_sql, default_kind)} END)"

        kind = "str" if "str" in kinds else (kinds.pop() if len(kinds) == 1 else "any")
        return DuckColumn(_render, kind)

    def stable_hash(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]

        def _render(ctx: Any) -> str:
            parts = ", ".join(f"COALESCE(CAST({x.sql(ctx)} AS VARCHAR), '')" for x in columns)
            return f"CAST((hash({parts}) & CAST({_SIGN_MASK} AS UBIGINT)) AS VARCHAR)"

        return DuckColumn(_render, "str")

    def rand(self, seed: int | None = None) -> DuckColumn:
        del seed  # DuckDB has a connection-level setseed(); per-call seeds are not portable.
        return DuckColumn.raw("random()", "num")

    def length(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"length(CAST({a} AS VARCHAR))", self._c(c), kind="num"
        )

    def to_double(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: spark_cast_sql(col.sql(ctx), "double"), "num")

    def row_number(self) -> DuckColumn:
        return DuckColumn.raw("row_number()", "num")

    def create_map(self, *cols: Any) -> DuckMapLiteral:
        """Spark ``create_map(k1, v1, k2, v2, …)`` over *literals* only.

        Accepts the flat form and the single-list form
        (``create_map([lit(k1), lit(v1), …])``). The result supports
        ``[key]`` lookup (see :class:`DuckMapLiteral`).
        """
        if len(cols) == 1 and isinstance(cols[0], (list, tuple)):
            cols = tuple(cols[0])
        if len(cols) % 2:
            raise ValueError("create_map expects an even number of key/value arguments")
        values = [_literal_value(c) for c in cols]
        pairs = list(zip(values[0::2], values[1::2], strict=True))
        return DuckMapLiteral(pairs, self._engine)

    def monotonically_increasing_id(self) -> DuckColumn:
        """Unique, non-negative BIGINT per row (``row_number() OVER () - 1``).

        Values differ from Spark's partition-encoded ids; only uniqueness
        within the result is guaranteed, as on Spark.
        """
        return DuckColumn.raw("(row_number() OVER () - 1)", "num")

    def year(self, c: Any) -> DuckColumn:
        """Spark ``year`` (strings are parsed as dates first; NULL on garbage)."""
        return DuckColumn.template(
            lambda a: f"CAST(year({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _c(c: Any) -> DuckColumn:
        from ovid2.core.engines._duck_column import to_column

        return to_column(c)

    #: ``Window`` (``fns.Window.partitionBy(...).orderBy(...)``).
    Window = DuckWindow


def _expr_output_name(text: str) -> str | None:
    match = re.search(r"\s+AS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*$", text, re.IGNORECASE)
    if match:
        return match.group(1)
    if re.fullmatch(r"\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*", text):
        return text.strip().strip("`")
    return None


class _DuckReader:
    """``session.read`` stand-in: ``csv`` / ``parquet`` / ``load`` of local files."""

    def __init__(self, engine: DuckDBEngine) -> None:
        self._engine = engine
        self._options: dict[str, Any] = {}
        self._format = "csv"

    def option(self, key: str, value: Any) -> _DuckReader:
        self._options[key.lower()] = value
        return self

    def options(self, **kwargs: Any) -> _DuckReader:
        for k, v in kwargs.items():
            self.option(k, v)
        return self

    def format(self, fmt: str) -> _DuckReader:
        self._format = fmt.lower()
        return self

    def csv(self, path: Any, **kwargs: Any) -> DuckFrame:
        for k, v in kwargs.items():
            self.option(k, v)
        header = str(self._options.get("header", "false")).lower() == "true"
        delimiter = str(self._options.get("sep") or self._options.get("delimiter") or ",")
        return self._engine.read_csv(path, delimiter=delimiter, header=header)

    def parquet(self, *paths: Any) -> DuckFrame:
        return self._engine.read_parquet(list(paths) if len(paths) > 1 else paths[0])

    def load(self, path: Any = None, format: str | None = None, **kwargs: Any) -> DuckFrame:
        fmt = (format or self._format).lower()
        for k, v in kwargs.items():
            self.option(k, v)
        if fmt == "parquet":
            return self.parquet(path)
        return self.csv(path)


class DuckDBEngineSession:
    """:class:`EngineSession` adapter over a DuckDB connection."""

    def __init__(self, engine: DuckDBEngine) -> None:
        self._engine = engine

    def table(self, name: str) -> DuckFrame:
        return self._engine.view(name)

    def sql(self, query: str) -> DuckFrame:
        return self._engine.wrap(self._engine.connection().sql(transpile_statement(query)))

    def createDataFrame(self, data: Any, schema: Any = None) -> DuckFrame:  # noqa: N802
        return self._engine.create_dataframe(data, schema)

    @property
    def read(self) -> _DuckReader:
        return _DuckReader(self._engine)

    @property
    def catalog(self) -> Any:
        return _DuckCatalog(self._engine)

    @property
    def native(self) -> Any:
        return self._engine.connection()


class _CatalogTable:
    def __init__(self, name: str) -> None:
        self.name = name
        self.isTemporary = True


class _DuckCatalog:
    def __init__(self, engine: DuckDBEngine) -> None:
        self._engine = engine

    def listTables(self) -> list[_CatalogTable]:  # noqa: N802 — PySpark API
        return [_CatalogTable(n) for n in self._engine.view_names()]

    def tableExists(self, name: str) -> bool:  # noqa: N802 — PySpark API
        return name.lower() in {n.lower() for n in self._engine.view_names()}


def transpile_statement(query: str) -> str:
    """Transpile a full Spark SQL statement (``SELECT …``) to DuckDB."""
    import sqlglot

    from ovid2.core.engines._duck_expr import _dialect, _rewrite

    tree = sqlglot.parse_one(query, dialect=_dialect())
    return str(tree.transform(_rewrite).sql(dialect="duckdb"))


_ALIAS_RE = re.compile(r"\b__ovq?\d+\b")


def _plan_key(frame: DuckFrame) -> str:
    """Return a cache key for ``frame``'s plan that ignores generated aliases.

    Every :class:`DuckFrame` takes a fresh ``__ov<N>`` / ``__ovq<N>``
    alias from a process-wide counter, so two *identical* plans built
    independently (the PCORnet->OMOP ``common`` fan-in, rebuilt by each
    domain formatter) render different SQL. Aliases are renumbered in
    order of first appearance — an alpha-renaming, so distinct plans
    stay distinct.
    """
    seen: dict[str, str] = {}

    def _canon(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"__a{len(seen)}")

    return _ALIAS_RE.sub(_canon, frame.sql_query())


def _default_threads() -> int:
    from ovid2.config import spark_sizing

    return max(1, spark_sizing.host_cpu_count())


def _default_memory_limit() -> str:
    from ovid2.config import spark_sizing

    total = spark_sizing.host_memory_gb()
    cap = _cgroup_memory_gb()
    if cap is not None:
        total = min(total, cap)
    gb = max(1, int(total * 0.75))
    return f"{gb}GB"


def _cgroup_memory_gb(path: str = "/sys/fs/cgroup/memory.max") -> int | None:
    """Return the cgroup v2 memory cap in GiB (``None`` when unlimited / absent).

    ``/proc/meminfo`` reports the host's RAM inside a container; the
    cgroup file is the real ceiling the kernel OOM-kills at.
    """
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return max(1, int(raw) // (1024**3))


class DuckDBEngine(BaseEngine):
    """Concrete :class:`BaseEngine` backed by an in-process DuckDB connection."""

    type_name = "duckdb"
    family = EngineFamily.DUCKDB
    session_is_spark = False
    supports_pandas_udf = False
    supports_broadcast_hint = False
    supports_input_file_name = True
    supports_local_files = True

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(args=args, secrets=secrets)
        self._conn: Any | None = None
        self._session: DuckDBEngineSession | None = None
        self._functions: _DuckFunctions | None = None
        self._views: dict[str, DuckFrame] = {}
        #: ``view name -> id(connection)`` the view's frame was built on.
        self._view_conns: dict[str, int] = {}
        #: ``id(frame) -> (scratch Parquet, frame)`` for frames rebound by export.
        self._rebind_targets: dict[int, tuple[str, DuckFrame]] = {}
        self._tables: dict[int, str] = {}
        self._lock = threading.RLock()
        #: Per-thread state: the table-worker cursor + checkpoint handles.
        self._local = threading.local()
        #: Live table-worker cursors (``id(thread) -> cursor``), for cancel / metrics.
        self._cursors: dict[int, Any] = {}
        #: ``thread id -> stage`` (see :attr:`current_stage`).
        self._stages: dict[int, str] = {}
        #: Keys being built right now by :meth:`_build_once` (``key -> Event``).
        self._inflight: dict[Any, threading.Event] = {}
        #: ``(conn token, cache id, plan key) -> scan frame`` (see :meth:`_shared_parquet`).
        self._scan_frames: dict[tuple[int, int, str], DuckFrame] = {}
        #: Number of threads currently inside :meth:`table_worker`.
        self._active_workers = 0
        #: Bumped by :meth:`stop_session` so stale per-thread cursors are dropped.
        self._generation = 0
        self._token_counter = itertools.count(1)  # noqa: OVID2001 — itertools, not a DataFrame action
        self._map_counter = itertools.count(0)  # noqa: OVID2001 — itertools, not a DataFrame action
        self._temp_dir: str | None = None
        self._started_ms: int | None = None
        self.rows_written_total = 0
        self.current_stage = None
        #: Cleanups registered by staging strategies (secrets, temp files).
        self._cleanups: list[Any] = []
        #: Arrow tables backing ``createDataFrame`` relations.
        self._arrow_refs: list[Any] = []
        #: ``source SQL -> (scratch dir, lineage)`` (see :meth:`stage_source`).
        self._staged: dict[str, tuple[str, frozenset[str]]] = {}
        #: ``plan SQL -> (scratch dir, lineage)`` (see :meth:`checkpoint`).
        self._shared_ckpts: dict[str, tuple[str, frozenset[str]]] = {}
        #: ``(id(connection), mapping signature) -> lookup table`` (``create_map``).
        self._lookup_tables: dict[tuple[int, str], str] = {}

    # ------------------------------------------------------------------
    # Thread-aware state (parallel tables)
    # ------------------------------------------------------------------

    @property
    def current_stage(self) -> str | None:
        """Active stage(s) — one per table worker, ``"; "``-joined."""
        stages = [s for s in list(self._stages.values()) if s]
        return "; ".join(stages) if stages else None

    @current_stage.setter
    def current_stage(self, value: str | None) -> None:
        key = threading.get_ident()
        with self._lock:
            if value:
                self._stages[key] = value
            else:
                self._stages.pop(key, None)

    def add_rows_written(self, rows: int) -> None:
        """Atomically add ``rows`` to :attr:`rows_written_total`."""
        with self._lock:
            self.rows_written_total += int(rows)

    def _checkpoints(self) -> list[Any]:
        """Return the *calling thread's* checkpoint handles (per-table)."""
        handles: list[Any] | None = getattr(self._local, "checkpoints", None)
        if handles is None:
            handles = []
            self._local.checkpoints = handles
        return handles

    def table_concurrency(self, n_tables: int) -> int:
        """Return how many tables may run at once (see ``table_concurrency``).

        DuckDB runs every query of a database on one shared thread pool,
        but many steps (CSV text writes, ``DISTINCT ON``, small tables)
        use a fraction of it. Running tables side by side on their own
        cursors keeps the pool busy. Default: ``min(n_tables, CPUs)``;
        ``table_concurrency`` / ``$OVID2_DUCKDB_TABLE_CONCURRENCY``
        overrides (``1`` = sequential).
        """
        raw = self.args.get("table_concurrency") or os.environ.get("OVID2_DUCKDB_TABLE_CONCURRENCY")
        try:
            wanted = int(raw) if raw not in (None, "") else _default_threads()
        except (TypeError, ValueError):
            _log.warning("Ignoring invalid table_concurrency=%r", raw)
            wanted = _default_threads()
        return max(1, min(wanted, max(1, int(n_tables))))

    @contextlib.contextmanager
    def table_worker(self) -> Iterator[None]:
        """Run the block on a private cursor of the run's database.

        Cursors share the catalog, scratch files and thread pool of the
        root connection but execute independently, so tables can run in
        parallel. Frames built on another connection are rebound on
        access (:meth:`view`, the staged / checkpoint caches).
        """
        root = self.connection()
        if getattr(self._local, "active", False):
            yield  # already inside a worker scope
            return
        # One cursor per worker *thread*, kept until ``stop_session``:
        # frames (and registered views) built inside the scope hold a
        # reference to it, so it must outlive the table that created it.
        cursor = getattr(self._local, "conn", None)
        if cursor is None or getattr(self._local, "generation", -1) != self._generation:
            cursor = root.cursor()
            with self._lock:
                token = next(self._token_counter)
                self._cursors[token] = cursor
            self._local.conn = cursor
            self._local.token = token
            self._local.generation = self._generation
        self._local.active = True
        # Views registered inside the scope (formatter source views, the
        # finished table) are private to it: two parallel tables must not
        # clobber each other's ``createOrReplaceTempView("src")``.
        self._local.views = {}
        with self._lock:
            self._active_workers += 1
        try:
            yield
        finally:
            self._local.active = False
            self._local.views = None
            with self._lock:
                self._active_workers -= 1

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def connection(self) -> Any:
        """Return the calling thread's ``duckdb`` connection.

        Inside :meth:`table_worker` this is the worker's private cursor;
        otherwise the root connection (created lazily).
        """
        local = getattr(self._local, "conn", None)
        if local is not None and getattr(self._local, "generation", -1) == self._generation:
            return local
        return self._root_connection()

    def _root_connection(self) -> Any:
        """Return the root connection, creating it lazily."""
        if self._conn is None:
            import duckdb

            database = str(self.args.get("database_path") or ":memory:")
            config: dict[str, Any] = {
                "threads": int(
                    self.args.get("threads")
                    or os.environ.get("OVID2_DUCKDB_THREADS")
                    or _default_threads()
                ),
                "memory_limit": str(
                    self.args.get("memory_limit")
                    or os.environ.get("OVID2_DUCKDB_MEMORY_LIMIT")
                    or _default_memory_limit()
                ),
                "preserve_insertion_order": str(
                    self.args.get("preserve_insertion_order", "false")
                ).lower()
                == "true",
            }
            ext_dir = os.environ.get("OVID2_DUCKDB_EXTENSION_DIR") or self.args.get(
                "extension_directory"
            )
            if ext_dir:
                # Image-baked extensions: never reach the network at run time.
                config["extension_directory"] = str(ext_dir)
                config["autoinstall_known_extensions"] = False
            self._temp_dir = str(
                self.args.get("temp_directory")
                or os.environ.get("OVID2_DUCKDB_TEMP_DIR")
                or tempfile.mkdtemp(prefix="ovid2-duckdb-", dir=_scratch_root())
            )
            Path(self._temp_dir).mkdir(parents=True, exist_ok=True)
            config["temp_directory"] = self._temp_dir
            self._conn = duckdb.connect(database, config=config)
            self._conn.execute("SET enable_progress_bar = true")
            self._conn.execute("SET enable_progress_bar_print = false")
            self._started_ms = int(time.monotonic() * 1000)
        return self._conn

    def get_session(self) -> EngineSession:
        """Return the session adapter (connection created lazily)."""
        if self._session is None:
            self.connection()
            self._session = DuckDBEngineSession(self)
        return self._session

    def stop_session(self) -> None:
        """Run staging cleanups, close the connection, drop the scratch dir."""
        from ovid2.core.engines.stager import run_session_cleanups

        if self._conn is not None:
            run_session_cleanups(self)
            with self._lock:
                cursors = list(self._cursors.values())
                self._cursors.clear()
            for cursor in cursors:
                with contextlib.suppress(Exception):
                    cursor.close()
            with contextlib.suppress(Exception):
                self._conn.close()
        self._conn = None
        self._session = None
        self._generation += 1
        import shutil

        exported = [t for t, _f in self._rebind_targets.values()]
        cached = [t for t, _l in [*self._staged.values(), *self._shared_ckpts.values()]]
        for target in [*self._tables.values(), *exported, *cached]:
            shutil.rmtree(target, ignore_errors=True)
        self._views.clear()
        self._view_conns.clear()
        self._staged.clear()
        self._shared_ckpts.clear()
        self._scan_frames.clear()
        self._lookup_tables.clear()
        self._stages.clear()
        self._tables.clear()
        self._rebind_targets.clear()
        if self._temp_dir and not self.args.get("temp_directory"):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None

    def native(self) -> Any:
        """Return the underlying ``duckdb.DuckDBPyConnection``."""
        return self.connection()

    def get_functions(self) -> EngineFunctions:
        """Return the cached :class:`EngineFunctions` adapter."""
        if self._functions is None:
            self._functions = _DuckFunctions(self)
        return self._functions

    def register_lookup_table(self, pairs: list[tuple[Any, Any]], *, stringify: bool) -> str:
        """Register ``pairs`` as a ``(__k VARCHAR, __v)`` Arrow table; return its name.

        Backs large :class:`DuckMapLiteral` lookups. Identical mappings
        share one table for the session's lifetime. Keys are stringified
        (the lookup casts the probe key to VARCHAR) and must be unique so
        the scalar subquery returns at most one row; on a duplicate the
        first value is kept.
        """
        import pyarrow as pa

        keys = [str(k) for k, _v in pairs]
        values = [None if v is None else (str(v) if stringify else v) for _k, v in pairs]
        # Registered Arrow tables are connection-local, so table workers
        # (own cursors) each register their own copy.
        signature = (self._conn_token(), repr((keys, values)))
        with self._lock:
            cached = self._lookup_tables.get(signature)
            if cached is not None:
                return cached
            seen: dict[str, Any] = {}
            for k, v in zip(keys, values, strict=True):
                seen.setdefault(k, v)
            name = f"__ovid2_map_{next(self._map_counter)}"
            table = pa.table({"__k": list(seen), "__v": list(seen.values())})
            self._arrow_refs.append(table)
            self.connection().register(name, table)
            self._lookup_tables[signature] = name
            return name

    # ------------------------------------------------------------------
    # Frames / views
    # ------------------------------------------------------------------

    def wrap(self, relation: Any, *, alias: str | None = None) -> DuckFrame:
        """Wrap a raw relation as a :class:`DuckFrame`."""
        return DuckFrame(self, relation, alias=alias)

    def register_view(self, name: str, frame: DuckFrame) -> None:
        """Register ``frame`` under ``name`` (Spark temp-view semantics).

        Spark temp views are *snapshots of the plan*: re-registering
        ``x`` with a plan that reads ``x`` must not recurse. We therefore
        keep the frame object itself (not a DuckDB ``VIEW``), so
        ``session.table(name)`` returns the plan that was registered.
        """
        scoped = getattr(self._local, "views", None)
        if scoped is not None:
            scoped[name.lower()] = frame
            return
        with self._lock:
            self._views[name.lower()] = frame
            self._view_conns[name.lower()] = self._conn_token()

    def view(self, name: str) -> DuckFrame:
        """Return the frame registered as ``name`` (Spark ``spark.table``).

        A frame registered on another connection (a reference table built
        before the parallel phase, read by a table worker) is rebound onto
        the caller's connection: DuckDB cannot combine relations of
        different connections.
        """
        key = name.lower()
        scoped: dict[str, DuckFrame] | None = getattr(self._local, "views", None)
        if scoped is not None and key in scoped:
            return scoped[key].alias(name)
        with self._lock:
            frame = self._views.get(key)
            token = self._view_conns.get(key, 0)
        if frame is None:
            raise LookupError(f"DuckDB engine: no table or view named {name!r}")
        if token != self._conn_token():
            frame = self._rebind(frame, key)
        return frame.alias(name)

    def view_names(self) -> list[str]:
        """Return every registered view name (global + the caller's worker scope)."""
        scoped = getattr(self._local, "views", None) or {}
        with self._lock:
            return sorted(set(self._views) | set(scoped))

    def _conn_token(self) -> int:
        """Return an id for the calling thread's connection (``0`` = root)."""
        if getattr(self._local, "generation", -1) != self._generation:
            return 0
        return int(getattr(self._local, "token", 0))

    def _parquet_frame(self, target: str, lineage: frozenset[str]) -> DuckFrame:
        """Return a frame scanning scratch Parquet ``target`` on this thread's connection."""
        rel = self.connection().read_parquet(os.path.join(target, "*.parquet"))
        return DuckFrame(self, rel, lineage=lineage)

    def _rebind(self, frame: DuckFrame, name: str) -> DuckFrame:
        """Return ``frame`` rebuilt on the calling thread's connection.

        1. Frames backed by scratch Parquet (reference tables, checkpoints)
           are re-scanned — cheap and exact.
        2. Otherwise the plan's SQL is re-bound on this connection (file
           scans, joins: exact, still lazy).
        3. Plans that cannot be re-bound (Arrow-backed ``createDataFrame``
           frames, connection-local lookup tables) are exported once to
           scratch Parquet on their own connection and re-scanned.
        """
        with self._lock:
            target = self._tables.get(id(frame))
            exported = self._rebind_targets.get(id(frame))
        if target is None and exported is not None and exported[1] is frame:
            target = exported[0]
        if target is None:
            try:
                rel = self.connection().sql(frame.sql_query())
                _ = rel.columns  # binding errors surface here
                return DuckFrame(self, rel, lineage=frame._lineage)
            except Exception as exc:  # duckdb errors are not importable here
                _log.debug("Re-binding view %r failed (%s); exporting it once.", name, exc)
            # The value keeps ``frame`` alive so its id cannot be reused.
            target, _frame = self._build_once(
                self._rebind_targets, id(frame), lambda: (self._export(frame, name), frame)
            )
        return self._parquet_frame(target, frame._lineage)

    def _export(self, frame: DuckFrame, name: str) -> str:
        """Execute ``frame`` on its own connection into scratch Parquet."""
        import shutil
        import uuid

        safe = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "view"
        base = self._temp_dir or tempfile.gettempdir()
        target = os.path.join(base, f"export-{safe}-{uuid.uuid4().hex[:8]}")
        os.makedirs(target, exist_ok=True)
        self.current_stage = f"export {safe}"
        try:
            # Deliberate materialisation boundary (cross-connection view).
            frame.relation.write_parquet(os.path.join(target, "data_0.parquet"))
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            self.current_stage = None
        return target

    def _build_once(self, cache: dict[Any, Any], key: Any, builder: Any) -> Any:
        """Return ``cache[key]``, building it at most once across threads.

        Concurrent callers asking for the same key wait for the first
        builder instead of repeating the work (two tables staging the same
        source). A failed build is not cached; the next caller retries.
        """
        slot = (id(cache), key)
        while True:
            with self._lock:
                if key in cache:
                    return cache[key]
                event = self._inflight.get(slot)
                owner = event is None
                if event is None:
                    event = threading.Event()
                    self._inflight[slot] = event
            if not owner:
                event.wait()
                continue
            try:
                value = builder()
                with self._lock:
                    cache[key] = value
                return value
            finally:
                with self._lock:
                    self._inflight.pop(slot, None)
                event.set()

    def materialise(self, frame: DuckFrame, name: str | None = None) -> DuckFrame:
        """Execute ``frame`` once into scratch Parquet and return a frame over it.

        A parallel ``COPY … (FORMAT parquet, PER_THREAD_OUTPUT)`` into the
        run's spill directory is ~3x faster than ``CREATE TEMP TABLE`` for
        wide all-VARCHAR rows, and reading it back is a cheap columnar scan.
        """
        import shutil
        import uuid

        conn = self.connection()
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", name or "ckpt").strip("_") or "ckpt"
        base = self._temp_dir or tempfile.gettempdir()
        target = os.path.join(base, f"ckpt-{safe}-{uuid.uuid4().hex[:8]}")
        view = f"__ovid2_ckpt_src_{uuid.uuid4().hex[:8]}"
        self.current_stage = f"checkpoint {safe}"
        frame.relation.create_view(view, replace=True)
        try:
            # Deliberate materialisation boundary (engine checkpoint).
            conn.execute(
                f"COPY (SELECT * FROM {ident(view)}) TO {jd.sql_string(target)} "
                "(FORMAT parquet, COMPRESSION 'lz4_raw', PER_THREAD_OUTPUT true, OVERWRITE true)"
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            with contextlib.suppress(Exception):
                conn.execute(f"DROP VIEW IF EXISTS {ident(view)}")
            self.current_stage = None
        rel = conn.read_parquet(os.path.join(target, "*.parquet"))
        out = DuckFrame(self, rel, lineage=frame._lineage)
        with self._lock:
            self._tables[id(out)] = target
        return out

    def stage_source(self, frame: DuckFrame, *, name: str) -> DuckFrame:
        """Parse a text source once into scratch Parquet (cached per run).

        DuckDB does not share a scan between the branches of a ``UNION``
        the way Spark's DAG does, so a source fanned out N ways (the
        PCORnet->OMOP ``VITAL`` leg splits 8 ways) would be re-parsed N
        times. Staging the parsed CSV as columnar Parquet makes every
        later scan cheap. Disabled with ``stage_sources=false`` /
        ``$OVID2_DUCKDB_STAGE_SOURCES=0``. Staged sources live until the
        session stops (shared across tables, like Spark's cached views).
        Thread-safe: parallel tables reading the same source parse it once.
        """
        if not self.stage_sources_enabled():
            return frame
        return self._shared_parquet(self._staged, frame, f"src_{name}")

    def _shared_parquet(
        self, cache: dict[str, tuple[str, frozenset[str]]], frame: DuckFrame, name: str
    ) -> DuckFrame:
        """Materialise ``frame`` once per plan; return a scan on this thread's connection."""
        key = _plan_key(frame)

        def _build() -> tuple[str, frozenset[str]]:
            out = self.materialise(frame, name)
            # The scratch dir now belongs to ``cache`` (removed at
            # ``stop_session``); ``out`` itself is not kept, so its id must
            # not linger in ``_tables``.
            with self._lock:
                target = self._tables.pop(id(out))
            return target, frame._lineage

        target, lineage = self._build_once(cache, key, _build)
        # One scan frame per (connection, plan): same-thread callers get
        # the identical frame back, as before parallel tables.
        frame_key = (self._conn_token(), id(cache), key)
        with self._lock:
            hit = self._scan_frames.get(frame_key)
        if hit is None:
            hit = self._parquet_frame(target, lineage)
            with self._lock:
                hit = self._scan_frames.setdefault(frame_key, hit)
        return hit

    def stage_sources_enabled(self) -> bool:
        """Return ``True`` when :meth:`stage_source` materialises eagerly."""
        flag = str(
            self.args.get("stage_sources", os.environ.get("OVID2_DUCKDB_STAGE_SOURCES", "1"))
        ).lower()
        return flag not in ("0", "false", "no", "off")

    def release(self, frame: DuckFrame) -> None:
        """Delete the scratch Parquet backing ``frame`` (if any)."""
        import shutil

        with self._lock:
            target = self._tables.pop(id(frame), None)
        if target is not None:
            shutil.rmtree(target, ignore_errors=True)

    def checkpoint(self, df: Any, name: str) -> Any:
        """Materialise ``df`` once into scratch Parquet (see :meth:`BaseEngine.checkpoint`).

        Checkpoints of an *identical* plan are reused across tables in the
        same run: the PCORnet->OMOP ``common`` fan-in is built verbatim by
        ``measurement``, ``observation`` and ``procedure_occurrence``. When
        those tables run in parallel the first builds it and the others
        wait. Shared checkpoints live until the session stops.
        """
        return self._shared_parquet(self._shared_ckpts, df, name)

    def release_checkpoints(self) -> int:
        """Drop every checkpoint table taken since the last release."""
        handles = self._checkpoints()
        released = 0
        while handles:
            self.release(handles.pop())
            released += 1
        return released

    def read_csv(
        self, path: Any, *, delimiter: str = ",", header: bool = True, **kw: Any
    ) -> DuckFrame:
        """Read CSV text as all-VARCHAR columns (Spark ``inferSchema=false``)."""
        from ovid2.core.engines.duckdb_stager import read_csv_relation

        return self.wrap(
            read_csv_relation(self.connection(), path, delimiter=delimiter, header=header, **kw)
        )

    def read_parquet(self, path: Any) -> DuckFrame:
        """Read Parquet file(s) / a Spark Parquet directory."""
        from ovid2.core.engines.duckdb_stager import parquet_glob

        return self.wrap(self.connection().read_parquet(parquet_glob(path)))

    def read_shared_view_file(self, source: Path, *, delimiter: str, header: bool) -> DuckFrame:
        """Read a file-sourced shared view (OMOP vocabulary) via the Parquet cache.

        Reuses the cache directory Spark builds (``part-*.parquet``) and
        builds it with DuckDB (``data_0.parquet``) when absent, so the
        multi-GB CONCEPT text is parsed once per file version across
        engines and runs. Falls back to a direct CSV scan when the cache
        is disabled or not writable.
        """
        from ovid2.core import vocab_cache

        cached = vocab_cache.cached_parquet(source, delimiter=delimiter, header=header)
        if cached is None:
            target = vocab_cache.build_target(source, delimiter=delimiter, header=header)
            if target is not None:
                temp, final = target
                try:
                    temp.mkdir(parents=True, exist_ok=True)
                    frame = self.read_csv(str(source), delimiter=delimiter, header=header)
                    self.current_stage = f"vocabulary cache {source.name}"
                    frame.relation.write_parquet(str(temp / "data_0.parquet"), compression="zstd")
                    cached = vocab_cache.publish(temp, final)
                except Exception as exc:
                    vocab_cache.discard(temp)
                    _log.warning("Vocabulary cache build failed for %s: %s", source, exc)
                    cached = None
                finally:
                    self.current_stage = None
        if cached is None:
            return self.read_csv(str(source), delimiter=delimiter, header=header)
        return self.read_parquet(str(cached))

    def create_dataframe(self, data: Any, schema: Any = None) -> DuckFrame:
        """``createDataFrame`` from rows (+ ``"a string, b int"`` / name list / StructType)."""
        names, types = _schema_columns(schema, data)
        return self._from_rows(list(data or []), names, types)

    def create_typed_dataframe(
        self,
        rows: list[tuple[Any, ...]],
        schema: list[tuple[str, str]],
    ) -> Any:
        """Build a DuckDB frame from rows + a portable schema."""
        names = [n for n, _t in schema]
        types = []
        for _n, t in schema:
            duck = _PORTABLE_DUCK_TYPES.get(t)
            if duck is None:
                raise ValueError(f"create_typed_dataframe: unknown portable type {t!r}")
            types.append(duck)
        return self._from_rows(rows, names, types)

    def _from_rows(self, rows: list[Any], names: list[str], types: list[str]) -> DuckFrame:
        import pyarrow as pa

        arrow_types = {
            "VARCHAR": pa.string(),
            "DOUBLE": pa.float64(),
            "BOOLEAN": pa.bool_(),
            "BIGINT": pa.int64(),
            "INTEGER": pa.int32(),
            "DATE": pa.date32(),
            "TIMESTAMP": pa.timestamp("us"),
        }
        columns: list[list[Any]] = [[] for _ in names]
        for row in rows:
            values = (
                row.asDict().values()
                if hasattr(row, "asDict")
                else ([row.get(n) for n in names] if isinstance(row, dict) else row)
            )
            for i, v in enumerate(values):
                columns[i].append(v)
        arrays = [
            pa.array(col, type=arrow_types.get(t.upper(), pa.string()))
            for col, t in zip(columns, types, strict=True)
        ]
        table = pa.Table.from_arrays(arrays, names=names)
        # ``from_arrow`` scans the Arrow buffers lazily; keep the table
        # alive for the engine's lifetime so the relation stays valid.
        self._arrow_refs.append(table)
        return self.wrap(self.connection().from_arrow(table))

    # ------------------------------------------------------------------
    # Portable DataFrame helpers
    # ------------------------------------------------------------------

    def union_by_name(self, left: Any, right: Any, *, allow_missing_columns: bool = False) -> Any:
        """``UNION ALL`` by name."""
        return left.unionByName(right, allowMissingColumns=allow_missing_columns)

    def drop_duplicates_keep_incomplete_keys(self, df: Any, cols: list[str]) -> Any:
        """One-pass null-key-passthrough dedup (``QUALIFY``).

        Equivalent to ``DISTINCT ON (cols)`` over complete-key rows
        ``UNION ALL`` incomplete-key rows, but the upstream plan runs
        once: DuckDB does not share a sub-plan across the two UNION
        branches, so the union form re-ran every formatter join / window
        (measured 71 s vs 15 s on a 60 M-row join plan).
        """
        return df.dropDuplicatesKeepIncompleteKeys(cols)

    def cross_join(self, left: Any, right: Any) -> Any:
        """Cartesian product."""
        return left.crossJoin(right)

    # ------------------------------------------------------------------
    # Probe / metrics / cancel
    # ------------------------------------------------------------------

    def test(self) -> EngineTestResult:
        """Open a connection and run ``SELECT 1``."""
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:
            return EngineTestResult(
                ok=False, code="unreachable", message=f"duckdb not importable: {exc}"
            )
        try:
            row = self.connection().execute("SELECT 1").fetchone()
            version = self.connection().execute("SELECT version()").fetchone()
        except Exception as exc:
            return EngineTestResult(ok=False, code="error", message=f"DuckDB probe failed: {exc}")
        if row and row[0] == 1:
            return EngineTestResult(
                ok=True,
                code="ok",
                message="DuckDB connection established",
                detail={"version": str(version[0]) if version else ""},
            )
        return EngineTestResult(
            ok=False, code="error", message="SELECT 1 returned unexpected result"
        )

    def get_metrics(self) -> EngineMetricsSample:
        """Cheap snapshot: query progress, memory, rows written."""
        from ovid2.core.engines.metrics import EngineMetricsSample

        now_ms = int(time.monotonic() * 1000)
        wall = datetime.now(UTC).isoformat()
        custom: dict[str, Any] = {}
        with self._lock:
            conns = [c for c in (self._conn, *self._cursors.values()) if c is not None]
        progresses: list[float] = []
        for conn in conns:
            with contextlib.suppress(Exception):
                progress = float(conn.query_progress())
                if progress >= 0:
                    progresses.append(progress)
        if progresses:
            # Several tables in flight: report the least-advanced query.
            custom["query_progress_pct"] = round(min(progresses), 1)
        if self._active_workers:
            custom["tables_in_flight"] = self._active_workers
        return EngineMetricsSample(
            t_ms=now_ms,
            wall_iso=wall,
            engine_family=self.family.value,
            current_stage=self.current_stage,
            rows_written_total=self.rows_written_total or None,
            custom=custom,
        )

    def cancel_running_queries(self) -> None:
        """Interrupt the running queries on every connection (best-effort, never raises)."""
        with self._lock:
            conns = [c for c in (self._conn, *self._cursors.values()) if c is not None]
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.interrupt()


def _scratch_root() -> str | None:
    """Return the real-disk scratch root shared with Spark (``SPARK_LOCAL_ROOT``)."""
    try:
        from ovid2.config.settings import SPARK_LOCAL_ROOT

        root = Path(os.environ.get("OVID2_DUCKDB_SCRATCH_ROOT") or SPARK_LOCAL_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    except OSError:
        return None


_SPARK_TYPE_TO_DUCK = {
    "string": "VARCHAR",
    "str": "VARCHAR",
    "int": "INTEGER",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "long": "BIGINT",
    "double": "DOUBLE",
    "float": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}


def _schema_columns(schema: Any, data: Any) -> tuple[list[str], list[str]]:
    """Return ``(names, duck types)`` for a ``createDataFrame`` schema argument."""
    if schema is None:
        rows = list(data or [])
        if rows and hasattr(rows[0], "asDict"):
            names = list(rows[0].asDict())
        elif rows and isinstance(rows[0], dict):
            # Spark infers a schema from a dict row with *sorted* keys.
            names = sorted(rows[0])
        else:
            width = len(rows[0]) if rows else 0
            names = [f"_{i + 1}" for i in range(width)]
        return names, ["VARCHAR"] * len(names)
    if isinstance(schema, str):
        names, types = [], []
        for part in schema.split(","):
            bits = part.strip().replace(":", " ").split()
            if not bits:
                continue
            names.append(bits[0].strip("`"))
            types.append(
                _SPARK_TYPE_TO_DUCK.get(bits[1].lower() if len(bits) > 1 else "string", "VARCHAR")
            )
        return names, types
    if isinstance(schema, (list, tuple)):
        return [str(s) for s in schema], ["VARCHAR"] * len(schema)
    fields = getattr(schema, "fields", None)
    if fields is not None:
        names = [f.name for f in fields]
        types = [
            _SPARK_TYPE_TO_DUCK.get(str(f.dataType.simpleString()).lower(), "VARCHAR")
            for f in fields
        ]
        return names, types
    raise TypeError(f"Unsupported createDataFrame schema {schema!r}")


__all__ = ["DuckDBEngine", "DuckDBEngineSession", "transpile_statement"]
