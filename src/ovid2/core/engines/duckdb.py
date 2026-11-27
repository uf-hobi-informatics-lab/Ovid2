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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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


#: ``key bytes -> (id(connection) -> SQL function name)`` for :meth:`_DuckFunctions.xor_base64`.
_XOR_KEY_FUNCTIONS: dict[bytes, dict[int, str]] = {}
_XOR_KEY_LOCK = threading.Lock()


def _xor_key_function(key: bytes) -> Any:
    """Return ``register(conn) -> name`` for a zero-arg SQL function yielding ``key`` as hex.

    The key stays out of the SQL text Ovid2 renders and logs; DuckDB folds
    the constant function once per query, so it costs nothing per row.
    """
    hex_key = key.hex()

    def _register(conn: Any) -> str:
        with _XOR_KEY_LOCK:
            per_conn = _XOR_KEY_FUNCTIONS.setdefault(key, {})
            name = per_conn.get(id(conn))
            if name is None:
                name = f"__ovid2_xor_key_{uuid.uuid4().hex[:12]}"
                conn.create_function(name, lambda: hex_key, [], "VARCHAR", side_effects=False)
                per_conn[id(conn)] = name
            return name

    return _register


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

    def xor_base64(self, c: Any, prefix: str, key: bytes, fallback: Any = None) -> DuckColumn:
        """``base64(xor(utf8(prefix || c), cyclic key))`` in native SQL (Ovid2 extension).

        The legacy id encryption (``ovid2.utils.common.encrypt_id_udf``)
        run as one vectorised DuckDB expression instead of a Python UDF —
        Python UDFs serialise on the GIL, so on a many-core host they are
        the whole run's bottleneck. Byte-identical to the Python
        ``b64encode(bytes(p[i] ^ key[i % len(key)] …))`` for string inputs
        (pinned by ``tests/unit/core/engines/test_duckdb_xor_base64.py``).

        The key is not spliced into the SQL Ovid2 builds (or logs): it is
        served by a zero-argument SQL function registered on the connection.

        Args:
            c: Input column (rendered with ``CAST(… AS VARCHAR)``).
            prefix: Non-empty plaintext prefix.
            key: Non-empty key bytes (cycled).
            fallback: Column used instead when ``c`` is not a string column
                (Python renders non-strings differently from DuckDB's cast).
        """
        if not prefix or not key:
            raise ValueError("xor_base64 needs a non-empty prefix and key")
        col = self._c(c)
        key_fn = _xor_key_function(key)
        key_len = len(key)
        prefix_sql = literal_sql(prefix)[0]

        def _render(ctx: Any) -> str:
            if fallback is not None and col.kind(ctx) not in ("str", "null"):
                # Unknown / non-string input: Python's ``f"{prefix}{v}"``
                # renders ints, floats … differently from a SQL cast.
                return cast("str", fallback.sql(ctx))
            from ovid2.core.engines.dsl import active_engine

            engine = active_engine()
            conn_fn = getattr(engine, "_root_connection", None)
            if conn_fn is None:
                raise RuntimeError("xor_base64 used without an active DuckDB engine")
            name = key_fn(conn_fn())
            p = f"encode({prefix_sql} || CAST({col.sql(ctx)} AS VARCHAR))"
            ks = f"unhex(substr(repeat({name}(), octet_length({p}) // {key_len} + 1), 1, 2 * octet_length({p})))"
            return f"to_base64(CAST(xor(CAST({p} AS BIT), CAST({ks} AS BIT)) AS BLOB))"

        return DuckColumn(_render, "str")

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

    # ---- portable surface (``ovid2.core.engines.surface``) --------------

    def udf(self, f: Any = None, returnType: Any = "string", **kwargs: Any) -> Any:  # noqa: N803
        """Spark ``udf`` (callable, decorator, or ``udf(returnType=...)``)."""
        from ovid2.core.engines._duck_udf import DuckUdf

        returnType = kwargs.pop("return_type", returnType)  # noqa: N806
        if f is None or not callable(f):
            rt = f if f is not None else returnType
            return lambda fn: DuckUdf(fn, rt)
        return DuckUdf(f, returnType)

    def pandas_udf(self, f: Any = None, returnType: Any = None, functionType: Any = None) -> Any:  # noqa: N803
        """Spark scalar ``pandas_udf`` (vectorised: Series in, Series out)."""
        from ovid2.core.engines._duck_udf import DuckUdf

        del functionType
        if f is None or not callable(f):
            rt = f if f is not None else (returnType or "string")
            return lambda fn: DuckUdf(fn, rt, vectorised=True)
        return DuckUdf(f, returnType or "string", vectorised=True)

    def lag(self, c: Any, offset: int = 1, default: Any = None) -> DuckColumn:
        col = self._c(c)
        dflt = "" if default is None else f", {literal_sql(default)[0]}"
        return DuckColumn(lambda ctx: f"lag({col.sql(ctx)}, {int(offset)}{dflt})", col.kind)

    def lead(self, c: Any, offset: int = 1, default: Any = None) -> DuckColumn:
        col = self._c(c)
        dflt = "" if default is None else f", {literal_sql(default)[0]}"
        return DuckColumn(lambda ctx: f"lead({col.sql(ctx)}, {int(offset)}{dflt})", col.kind)

    def rank(self) -> DuckColumn:
        return DuckColumn.raw("rank()", "num")

    def dense_rank(self) -> DuckColumn:
        return DuckColumn.raw("dense_rank()", "num")

    def percent_rank(self) -> DuckColumn:
        return DuckColumn.raw("percent_rank()", "num")

    def cume_dist(self) -> DuckColumn:
        return DuckColumn.raw("cume_dist()", "num")

    def ntile(self, n: int) -> DuckColumn:
        return DuckColumn.raw(f"ntile({int(n)})", "num")

    def last(self, c: Any, ignorenulls: bool = False) -> DuckColumn:
        col = self._c(c)
        fn = "last" if not ignorenulls else "arg_max_null"
        if ignorenulls:
            return DuckColumn(lambda ctx: f"last({col.sql(ctx)} IGNORE NULLS)", col.kind)
        del fn
        return DuckColumn(lambda ctx: f"last({col.sql(ctx)})", col.kind)

    def lpad(self, c: Any, length: int, pad: str) -> DuckColumn:
        p = jd.sql_string(pad)
        # Spark: pad / truncate to ``length``; DuckDB ``lpad`` matches.
        return DuckColumn.template(
            lambda a: f"lpad(CAST({a} AS VARCHAR), {int(length)}, {p})", self._c(c), kind="str"
        )

    def rpad(self, c: Any, length: int, pad: str) -> DuckColumn:
        p = jd.sql_string(pad)
        return DuckColumn.template(
            lambda a: f"rpad(CAST({a} AS VARCHAR), {int(length)}, {p})", self._c(c), kind="str"
        )

    def instr(self, c: Any, substr: str) -> DuckColumn:
        s_ = jd.sql_string(substr)
        return DuckColumn.template(
            lambda a: f"CAST(instr(CAST({a} AS VARCHAR), {s_}) AS INTEGER)", self._c(c), kind="num"
        )

    def locate(self, substr: str, c: Any, pos: int = 1) -> DuckColumn:
        s_ = jd.sql_string(substr)
        start = max(1, int(pos))
        return DuckColumn.template(
            lambda a: (
                f"CASE WHEN {a} IS NULL THEN NULL ELSE CAST(COALESCE(NULLIF(instr("
                f"substring(CAST({a} AS VARCHAR), {start}), {s_}), 0) + {start - 1}, 0) AS INTEGER) END"
            ),
            self._c(c),
            kind="num",
        )

    def isnan(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"COALESCE(isnan(TRY_CAST({a} AS DOUBLE)), FALSE)", self._c(c), kind="bool"
        )

    def isnull(self, c: Any) -> DuckColumn:
        return self._c(c).isNull()

    def nanvl(self, a: Any, b: Any) -> DuckColumn:
        x, y = self._c(a), self._c(b)
        return DuckColumn(
            lambda ctx: f"CASE WHEN isnan({x.sql(ctx)}) THEN {y.sql(ctx)} ELSE {x.sql(ctx)} END",
            "num",
        )

    def nvl(self, a: Any, b: Any) -> DuckColumn:
        return self.coalesce(a, b)

    ifnull = nvl

    def nullif(self, a: Any, b: Any) -> DuckColumn:
        x, y = self._c(a), DuckColumn.literal(b) if not isinstance(b, DuckColumn) else b
        return DuckColumn(lambda ctx: f"NULLIF({x.sql(ctx)}, {y.sql(ctx)})", x.kind)

    def greatest(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        return DuckColumn(lambda ctx: "greatest(" + ", ".join(c.sql(ctx) for c in columns) + ")")

    def least(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        return DuckColumn(lambda ctx: "least(" + ", ".join(c.sql(ctx) for c in columns) + ")")

    def floor(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(floor(TRY_CAST({a} AS DOUBLE)) AS BIGINT)", self._c(c), kind="num"
        )

    def ceil(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(ceil(TRY_CAST({a} AS DOUBLE)) AS BIGINT)", self._c(c), kind="num"
        )

    ceiling = ceil

    def sqrt(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"sqrt(TRY_CAST({a} AS DOUBLE))", self._c(c), kind="num"
        )

    def pow(self, a: Any, b: Any) -> DuckColumn:
        x, y = self._c(a), to_col_or_lit(b)
        return DuckColumn(
            lambda ctx: f"pow(TRY_CAST({x.sql(ctx)} AS DOUBLE), TRY_CAST({y.sql(ctx)} AS DOUBLE))",
            "num",
        )

    def exp(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"exp(TRY_CAST({a} AS DOUBLE))", self._c(c), kind="num"
        )

    def log(self, *args: Any) -> DuckColumn:
        if len(args) == 2:
            base, c = args
            return DuckColumn.template(
                lambda a: f"(ln(TRY_CAST({a} AS DOUBLE)) / ln({float(base)}))",
                self._c(c),
                kind="num",
            )
        return DuckColumn.template(
            lambda a: f"ln(TRY_CAST({a} AS DOUBLE))", self._c(args[0]), kind="num"
        )

    def bround(self, c: Any, scale: int = 0) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"round_even(TRY_CAST({a} AS DOUBLE), {int(scale)})", self._c(c), kind="num"
        )

    def signum(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(sign(TRY_CAST({a} AS DOUBLE)) AS DOUBLE)", self._c(c), kind="num"
        )

    def collect_list(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(
            lambda ctx: f"list({col.sql(ctx)}) FILTER (WHERE {col.sql(ctx)} IS NOT NULL)"
        )

    def collect_set(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(
            lambda ctx: f"list(DISTINCT {col.sql(ctx)}) FILTER (WHERE {col.sql(ctx)} IS NOT NULL)"
        )

    def array_contains(self, c: Any, value: Any) -> DuckColumn:
        col, v = self._c(c), DuckColumn.literal(value)
        return DuckColumn(lambda ctx: f"list_contains({col.sql(ctx)}, {v.sql(ctx)})", "bool")

    def size(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"CAST(COALESCE(len({col.sql(ctx)}), -1) AS INTEGER)", "num")

    def array_join(self, c: Any, delimiter: str, null_replacement: str | None = None) -> DuckColumn:
        col = self._c(c)
        d = jd.sql_string(delimiter)
        if null_replacement is None:
            return DuckColumn(
                lambda ctx: (
                    f"array_to_string(list_filter({col.sql(ctx)}, x -> x IS NOT NULL), {d})"
                ),
                "str",
            )
        r = jd.sql_string(null_replacement)
        return DuckColumn(
            lambda ctx: (
                f"array_to_string(list_transform({col.sql(ctx)}, x -> COALESCE(CAST(x AS VARCHAR), {r})), {d})"
            ),
            "str",
        )

    def struct(self, *cols: Any) -> DuckColumn:
        if len(cols) == 1 and isinstance(cols[0], (list, tuple)):
            cols = tuple(cols[0])
        columns = [self._c(c) for c in cols]

        def _render(ctx: Any) -> str:
            parts = []
            for i, c in enumerate(columns):
                name = c.output_name or f"col{i + 1}"
                parts.append(f"{ident(name)} := {c.sql(ctx)}")
            return "struct_pack(" + ", ".join(parts) + ")"

        return DuckColumn(_render)

    named_struct = struct

    def format_string(self, fmt: str, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        f = jd.sql_string(fmt)
        return DuckColumn(
            lambda ctx: f"printf({f}" + "".join(", " + c.sql(ctx) for c in columns) + ")", "str"
        )

    printf = format_string

    def translate(self, c: Any, matching: str, replace: str) -> DuckColumn:
        m, r = jd.sql_string(matching), jd.sql_string(replace)
        return DuckColumn.template(
            lambda a: f"translate(CAST({a} AS VARCHAR), {m}, {r})", self._c(c), kind="str"
        )

    def reverse(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"reverse(CAST({a} AS VARCHAR))", self._c(c), kind="str"
        )

    def repeat(self, c: Any, n: int) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"repeat(CAST({a} AS VARCHAR), {int(n)})", self._c(c), kind="str"
        )

    def ascii(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"ascii(CAST({a} AS VARCHAR))", self._c(c), kind="num")

    def base64(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"to_base64(encode(CAST({a} AS VARCHAR)))", self._c(c), kind="str"
        )

    def hash(self, *cols: Any) -> DuckColumn:
        columns = [self._c(c) for c in cols]
        return DuckColumn(
            lambda ctx: "CAST(hash(" + ", ".join(c.sql(ctx) for c in columns) + ") AS BIGINT)",
            "num",
        )

    def xxhash64(self, *cols: Any) -> DuckColumn:
        return self.hash(*cols)

    def crc32(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(hash(CAST({a} AS VARCHAR)) % 4294967296 AS BIGINT)",
            self._c(c),
            kind="num",
        )

    def sha1(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"sha1(CAST({a} AS VARCHAR))", self._c(c), kind="str")

    def month(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(month({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    def dayofmonth(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(day({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    day = dayofmonth

    def hour(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(hour({a}) AS INTEGER)", self.to_timestamp(c), kind="num"
        )

    def minute(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(minute({a}) AS INTEGER)", self.to_timestamp(c), kind="num"
        )

    def second(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(second({a}) AS INTEGER)", self.to_timestamp(c), kind="num"
        )

    def dayofweek(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(dayofweek({a}) + 1 AS INTEGER)", self.to_date(c), kind="num"
        )

    def dayofyear(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(dayofyear({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    def quarter(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(quarter({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    def weekofyear(self, c: Any) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST(weekofyear({a}) AS INTEGER)", self.to_date(c), kind="num"
        )

    def date_sub(self, c: Any, days: int) -> DuckColumn:
        return self.date_add(c, -int(days))

    def add_months(self, c: Any, months: int) -> DuckColumn:
        return DuckColumn.template(
            lambda a: f"CAST({a} + INTERVAL ({int(months)}) MONTH AS DATE)",
            self.to_date(c),
            kind="date",
        )

    def months_between(self, end: Any, start: Any, roundOff: bool = True) -> DuckColumn:  # noqa: N803
        e, s_ = self.to_timestamp(end), self.to_timestamp(start)
        digits = 8 if roundOff else 15
        return DuckColumn(
            lambda ctx: f"round((date_diff('day', {s_.sql(ctx)}, {e.sql(ctx)})) / 31.0, {digits})",
            "num",
        )

    def last_day(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"last_day({a})", self.to_date(c), kind="date")

    def trunc(self, c: Any, fmt: str) -> DuckColumn:
        unit = {
            "year": "year",
            "yyyy": "year",
            "yy": "year",
            "month": "month",
            "mon": "month",
            "mm": "month",
            "quarter": "quarter",
            "week": "week",
        }.get(fmt.lower(), "month")
        return DuckColumn.template(
            lambda a: f"CAST(date_trunc('{unit}', {a}) AS DATE)", self.to_date(c), kind="date"
        )

    def date_trunc(self, fmt: str, c: Any) -> DuckColumn:
        unit = {"yyyy": "year", "yy": "year", "mon": "month", "mm": "month", "dd": "day"}.get(
            fmt.lower(), fmt.lower()
        )
        return DuckColumn.template(
            lambda a: f"date_trunc('{unit}', {a})", self.to_timestamp(c), kind="ts"
        )

    def unix_timestamp(self, c: Any = None, fmt: str = "yyyy-MM-dd HH:mm:ss") -> DuckColumn:
        if c is None:
            return DuckColumn.raw("CAST(epoch(current_localtimestamp()) AS BIGINT)", "num")
        return DuckColumn.template(
            lambda a: f"CAST(epoch({a}) AS BIGINT)", self.to_timestamp(c, fmt), kind="num"
        )

    def from_unixtime(self, c: Any, fmt: str = "yyyy-MM-dd HH:mm:ss") -> DuckColumn:
        f = jd.sql_string(jd.strftime_format(fmt))
        return DuckColumn.template(
            lambda a: f"strftime(to_timestamp(TRY_CAST({a} AS BIGINT))::TIMESTAMP, {f})",
            self._c(c),
            kind="str",
        )

    def from_utc_timestamp(self, c: Any, tz: str) -> DuckColumn:
        z = jd.sql_string(tz)
        return DuckColumn.template(
            lambda a: (
                f"CAST(timezone({z}, CAST({a} AS TIMESTAMP) AT TIME ZONE 'UTC') AS TIMESTAMP)"
            ),
            self.to_timestamp(c),
            kind="ts",
        )

    def to_utc_timestamp(self, c: Any, tz: str) -> DuckColumn:
        z = jd.sql_string(tz)
        return DuckColumn.template(
            lambda a: (
                f"CAST(timezone('UTC', CAST({a} AS TIMESTAMP) AT TIME ZONE {z}) AS TIMESTAMP)"
            ),
            self.to_timestamp(c),
            kind="ts",
        )

    def to_csv(self, c: Any, options: dict[str, str] | None = None) -> DuckColumn:
        """Spark ``to_csv(struct(...))`` (univocity writer rules, every field as text)."""
        from ovid2.core.engines._duck_udf import DuckUdf

        opts = dict(options or {})
        sep = str(opts.get("sep") or opts.get("delimiter") or ",")
        quote = opts.get("quote", '"')
        if quote not in ("", "\x00"):
            raise EngineOperationUnsupported(
                "duckdb", "to_csv", "only quote='' (Spark's unquoted writer) is portable"
            )
        line_start = str(opts.get("comment", "#")) == "#"
        col = self._c(c)
        writer = DuckUdf(lambda row: _to_csv_row(row, sep, line_start), "string")
        return writer(col)

    def sort_array(self, c: Any, asc: bool = True) -> DuckColumn:
        col = self._c(c)
        order = "'ASC'" if asc else "'DESC'"
        return DuckColumn(lambda ctx: f"list_sort({col.sql(ctx)}, {order}, 'NULLS FIRST')")

    def element_at(self, c: Any, idx: Any) -> DuckColumn:
        col = self._c(c)
        if isinstance(idx, int):
            return DuckColumn(lambda ctx: f"({col.sql(ctx)})[{int(idx)}]")
        key = DuckColumn.literal(idx) if not isinstance(idx, DuckColumn) else idx
        return DuckColumn(lambda ctx: f"element_at({col.sql(ctx)}, {key.sql(ctx)})[1]")

    def asc(self, name: str) -> DuckColumn:
        return self.col(name).asc()

    def desc(self, name: str) -> DuckColumn:
        return self.col(name).desc()

    def asc_nulls_last(self, name: str) -> DuckColumn:
        return self.col(name).asc_nulls_last()

    def desc_nulls_first(self, name: str) -> DuckColumn:
        return self.col(name).desc_nulls_first()

    def desc_nulls_last(self, name: str) -> DuckColumn:
        return self.col(name).desc()

    def asc_nulls_first(self, name: str) -> DuckColumn:
        return self.col(name).asc()

    def mean(self, c: Any) -> DuckColumn:
        return self.avg(c)

    def stddev(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"stddev_samp(TRY_CAST({col.sql(ctx)} AS DOUBLE))", "num")

    stddev_samp = stddev

    def variance(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"var_samp(TRY_CAST({col.sql(ctx)} AS DOUBLE))", "num")

    var_samp = variance

    def approx_count_distinct(self, c: Any, rsd: float = 0.05) -> DuckColumn:
        del rsd
        col = self._c(c)
        return DuckColumn(
            lambda ctx: f"CAST(approx_count_distinct({col.sql(ctx)}) AS BIGINT)", "num"
        )

    def sumDistinct(self, c: Any) -> DuckColumn:  # noqa: N802 — PySpark API
        col = self._c(c)
        return DuckColumn(lambda ctx: f"sum(DISTINCT TRY_CAST({col.sql(ctx)} AS DOUBLE))", "num")

    sum_distinct = sumDistinct

    def count_distinct(self, *cols: Any) -> DuckColumn:
        return self.countDistinct(*cols)

    def bool_and(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"bool_and({col.sql(ctx)})", "bool")

    def bool_or(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"bool_or({col.sql(ctx)})", "bool")

    def flatten(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"flatten({col.sql(ctx)})")

    def array_distinct(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"list_distinct({col.sql(ctx)})")

    def posexplode(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(lambda ctx: f"unnest({col.sql(ctx)})")

    def explode_outer(self, c: Any) -> DuckColumn:
        col = self._c(c)
        return DuckColumn(
            lambda ctx: (
                f"unnest(CASE WHEN len({col.sql(ctx)}) > 0 THEN {col.sql(ctx)} ELSE [NULL] END)"
            )
        )

    def typeof(self, c: Any) -> DuckColumn:
        return DuckColumn.template(lambda a: f"lower(typeof({a}))", self._c(c), kind="str")

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _c(c: Any) -> DuckColumn:
        from ovid2.core.engines._duck_column import to_column

        return to_column(c)

    #: ``Window`` (``fns.Window.partitionBy(...).orderBy(...)``).
    Window = DuckWindow


def to_col_or_lit(value: Any) -> DuckColumn:
    """A column for ``value``: columns pass through, everything else is a literal."""
    return value if isinstance(value, DuckColumn) else DuckColumn.literal(value)


def _to_csv_row(row: Any, sep: str, line_start: bool) -> str | None:
    """``to_csv`` of one struct row (Spark writer rules; values rendered as Spark text)."""
    from ovid2.core.engines._duck_udf import python_value_text
    from ovid2.core.engines.csv_text import write_cell

    if row is None:
        return None
    values = list(row.values()) if isinstance(row, dict) else [row]
    out = []
    for i, v in enumerate(values):
        text = python_value_text(v)
        cell = write_cell(text, delimiter=sep, line_start=line_start and i == 0)
        out.append("" if cell is None else cell)
    return sep.join(out)


def _expr_output_name(text: str) -> str | None:
    match = re.search(r"\s+AS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*$", text, re.IGNORECASE)
    if match:
        return match.group(1)
    if re.fullmatch(r"\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*", text):
        return text.strip().strip("`")
    return None


class _DuckReader:
    """``session.read`` stand-in: ``csv`` / ``parquet`` / ``load`` of local files.

    Option semantics follow Spark's ``DataFrameReader``:

    * **Disc-driver reads** (``ignoreLeadingWhiteSpace`` and
      ``ignoreTrailingWhiteSpace`` both true — the Ovid2 disc contract)
      use DuckDB's parallel CSV scanner (values trimmed, empty → NULL).
    * **Every other CSV read** (Spark defaults: no trimming,
      ``multiLine=false``, ``escape='\\'`` unless set,
      ``inferSchema=false``) goes through the univocity model
      (:mod:`ovid2.core.engines.csv_text`), byte-exact with Spark.
    * ``inferSchema=true`` applies Spark's CSV schema inference
      (:mod:`ovid2.legacy.spark33_infer`) and returns typed columns.
    """

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

    def schema(self, schema: Any) -> _DuckReader:
        self._options["__schema"] = schema
        return self

    def _flag(self, key: str, default: bool = False) -> bool:
        value = self._options.get(key.lower())
        if value is None:
            return default
        return str(value).lower() == "true"

    def csv(self, path: Any, **kwargs: Any) -> DuckFrame:
        for k, v in kwargs.items():
            if v is not None:
                self.option(k, v)
        header = self._flag("header")
        delimiter = str(self._options.get("sep") or self._options.get("delimiter") or ",")
        if self._flag("ignoreleadingwhitespace") and self._flag("ignoretrailingwhitespace"):
            return self._engine.read_csv(path, delimiter=delimiter, header=header)
        from ovid2.core.engines.duckdb_stager import read_spark_default_csv

        paths = [str(p) for p in path] if isinstance(path, (list, tuple)) else [str(path)]
        escape = str(self._options.get("escape") or "\\")
        quote = str(self._options.get("quote", '"') or "\x00")
        frames = [
            read_spark_default_csv(
                self._engine,
                p,
                delimiter=delimiter,
                quote=quote,
                escape=escape,
                header=header,
                ignore_leading=self._flag("ignoreleadingwhitespace"),
                ignore_trailing=self._flag("ignoretrailingwhitespace"),
            )
            for p in paths
        ]
        frame = frames[0]
        for extra in frames[1:]:
            frame = frame.union(extra)
        if self._flag("inferschema"):
            from ovid2.legacy.spark33_infer import infer_typed

            frame = infer_typed(frame)
        return frame

    def parquet(self, *paths: Any) -> DuckFrame:
        return self._engine.read_parquet(list(paths) if len(paths) > 1 else paths[0])

    def text(self, path: Any) -> DuckFrame:
        from ovid2.legacy.text_io import read_lines

        return cast(DuckFrame, read_lines(self._engine.get_session(), str(path)))

    def json(self, path: Any) -> DuckFrame:
        return self._engine.wrap(self._engine.connection().read_json(str(path)))

    def load(self, path: Any = None, format: str | None = None, **kwargs: Any) -> DuckFrame:
        fmt = (format or self._format).lower()
        for k, v in kwargs.items():
            if v is not None:
                self.option(k, v)
        if fmt == "parquet":
            return self.parquet(path)
        if fmt == "json":
            return self.json(path)
        if fmt == "text":
            return self.text(path)
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

    def from_arrow(self, table: Any) -> DuckFrame:
        """Wrap a ``pyarrow.Table`` (kept alive for the engine's lifetime)."""
        self._engine._arrow_refs.append(table)
        return self._engine.wrap(self._engine.connection().from_arrow(table))

    @property
    def udf(self) -> _DuckUdfRegistration:
        """``spark.udf.register(name, fn)`` — SQL-callable Python functions."""
        return _DuckUdfRegistration(self._engine)

    @property
    def conf(self) -> _DuckConf:
        """``spark.conf`` stand-in (``get`` / ``set`` / ``unset`` of plain keys)."""
        return self._engine._conf

    @property
    def sparkContext(self) -> _DuckSparkContext:  # noqa: N802 — PySpark API
        """Minimal ``SparkContext`` (``broadcast`` / ``emptyRDD`` / ``setLogLevel``)."""
        return _DuckSparkContext()

    def stop(self) -> None:
        """Sessions are owned by the engine; ``stop`` is a no-op."""

    @property
    def read(self) -> _DuckReader:
        return _DuckReader(self._engine)

    @property
    def catalog(self) -> Any:
        return _DuckCatalog(self._engine)

    @property
    def native(self) -> Any:
        return self._engine.connection()


class _DuckUdfRegistration:
    """``session.udf`` for DuckDB: register a Python UDF as a SQL function."""

    def __init__(self, engine: DuckDBEngine) -> None:
        self._engine = engine

    def register(self, name: str, f: Any, returnType: Any = None) -> Any:  # noqa: N803
        """Register ``f`` under ``name`` (any arity; Spark ``udf.register``)."""
        from ovid2.core.engines._duck_udf import DuckUdf

        udf = (
            f
            if isinstance(f, DuckUdf)
            else DuckUdf(getattr(f, "func", f), returnType or getattr(f, "returnType", "string"))
        )
        udf.register_as(self._engine._root_connection(), name)
        self._engine._sql_udfs[name.lower()] = udf
        return udf


class _DuckConf:
    """``spark.conf``: a plain key/value store (no engine semantics)."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = str(value)

    def unset(self, key: str) -> None:
        self._values.pop(key, None)

    def getAll(self) -> dict[str, str]:  # noqa: N802 — PySpark API
        return dict(self._values)


class _Broadcast:
    """``SparkContext.broadcast`` result: ``.value`` holds the object."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def unpersist(self, blocking: bool = False) -> None:
        del blocking

    def destroy(self) -> None:
        """No-op (driver-side object)."""


class _DuckSparkContext:
    """The parts of ``SparkContext`` driver code touches."""

    def broadcast(self, value: Any) -> _Broadcast:
        return _Broadcast(value)

    def emptyRDD(self) -> list[Any]:  # noqa: N802 — PySpark API
        return []

    def setLogLevel(self, level: str) -> None:  # noqa: N802 — PySpark API
        del level

    @property
    def defaultParallelism(self) -> int:  # noqa: N802 — PySpark API
        return _default_threads()


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


_ALIAS_RE = re.compile(r"\b(?:__ovq?\d+|unnamed_relation_[0-9a-f]+)\b")


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
        #: ``spark.conf`` stand-in shared by every session handle.
        self._conf = _DuckConf()
        #: SQL-registered Python UDFs (``session.udf.register``).
        self._sql_udfs: dict[str, Any] = {}

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

    @contextlib.contextmanager
    def table_worker(self) -> Iterator[None]:
        """Run the block on a private cursor of the run's database.

        Pipelines run tables one at a time on the root connection; this
        scope exists for callers that touch the engine from another
        thread (a DuckDB connection must not be shared by threads running
        queries at once). Cursors share the catalog, scratch files and
        thread pool of the root connection. Frames built on another
        connection are rebound on access (:meth:`view`, the staged /
        checkpoint caches).
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
            # TEMP macros are connection-local: a cursor does not see the
            # root's ``__ovid2_java_double`` (``CAST(double AS STRING)``).
            from ovid2.core.engines.duckdb_stager import _shortest_double_text

            _shortest_double_text(cursor)
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
            from ovid2.core.engines.duckdb_stager import _shortest_double_text

            # ``CAST(double AS STRING)`` renders Java ``Double.toString``
            # through this macro (``_duck_expr._to_string_sql``).
            _shortest_double_text(self._conn)
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
            from ovid2.core.engines._duck_udf import forget_connection

            run_session_cleanups(self)
            forget_connection(self._conn)
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

    def warm_reference(self, session: Any, df: Any, name: str) -> Any:
        """Execute a reference table once into scratch Parquet (see :meth:`materialise`).

        DuckDB re-runs a plan per consumer (no shared DAG), so without this
        every clinical table re-ran the reference table's whole plan inside
        its own write.
        """
        del session
        return self.materialise(df, f"ref_{name}")

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
                f"(FORMAT parquet, COMPRESSION {jd.sql_string(self._scratch_codec())}, "
                "PER_THREAD_OUTPUT true, OVERWRITE true)"
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

    #: Parquet codecs accepted for scratch checkpoints.
    _SCRATCH_CODECS: frozenset[str] = frozenset({"zstd", "lz4_raw", "snappy", "uncompressed"})

    def _scratch_codec(self) -> str:
        """Return the scratch Parquet codec (``scratch_codec`` / ``$OVID2_DUCKDB_SCRATCH_CODEC``).

        ``zstd`` by default: on ACME's 52 GB ``observation.csv`` the
        parse+write and the re-scan cost the same as ``lz4_raw`` while
        the files are ~37% smaller, so less scratch I/O on a shared disk.
        """
        raw = str(
            self.args.get("scratch_codec") or os.environ.get("OVID2_DUCKDB_SCRATCH_CODEC") or "zstd"
        ).lower()
        if raw not in self._SCRATCH_CODECS:
            _log.warning("Ignoring unknown scratch_codec=%r; using zstd", raw)
            return "zstd"
        return raw

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

    def read_shared_view_file(
        self, source: Path, *, delimiter: str, header: bool, bus: Any = None
    ) -> DuckFrame:
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
            "FLOAT": pa.float32(),
            "BOOLEAN": pa.bool_(),
            "BIGINT": pa.int64(),
            "INTEGER": pa.int32(),
            "SMALLINT": pa.int16(),
            "TINYINT": pa.int8(),
            "DATE": pa.date32(),
            "TIMESTAMP": pa.timestamp("us"),
            "BLOB": pa.binary(),
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

        def _arrow_type(duck: str) -> Any:
            upper = duck.upper()
            if upper.startswith("DECIMAL"):
                inner = upper[len("DECIMAL") :].strip("() ")
                p_, _, s_ = inner.partition(",")
                return pa.decimal128(int(p_ or 38), int(s_ or 0))
            return arrow_types.get(upper, pa.string())

        arrays = [pa.array(col, type=_arrow_type(t)) for col, t in zip(columns, types, strict=True)]
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
    "void": "VARCHAR",
    "tinyint": "TINYINT",
    "smallint": "SMALLINT",
    "int": "INTEGER",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "long": "BIGINT",
    "double": "DOUBLE",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamp_ntz": "TIMESTAMP",
    "binary": "BLOB",
}


def _infer_duck_type(values: list[Any]) -> str:
    """Spark ``createDataFrame`` schema inference from Python values → DuckDB type."""
    import datetime as _dt
    from decimal import Decimal

    seen = [v for v in values if v is not None]
    if not seen:
        return "VARCHAR"
    v = seen[0]
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "BIGINT"
    if isinstance(v, float):
        return "DOUBLE"
    if isinstance(v, Decimal):
        return "DECIMAL(38,18)"
    if isinstance(v, _dt.datetime):
        return "TIMESTAMP"
    if isinstance(v, _dt.date):
        return "DATE"
    return "VARCHAR"


def _row_values(row: Any, names: list[str]) -> list[Any]:
    if hasattr(row, "asDict"):
        return list(row.asDict().values())
    if isinstance(row, dict):
        return [row.get(n) for n in names]
    return list(row)


def _schema_columns(schema: Any, data: Any) -> tuple[list[str], list[str]]:
    """Return ``(names, duck types)`` for a ``createDataFrame`` schema argument.

    Mirrors Spark: a name list / no schema infers each column's type from
    the Python values (``bool`` → boolean, ``int`` → bigint, ``float`` →
    double, ``datetime`` → timestamp …); a DDL string or a
    ``StructType`` (portable or PySpark) gives the types explicitly.
    """
    rows = list(data or [])
    if schema is None or (
        isinstance(schema, (list, tuple)) and all(isinstance(s, str) for s in schema)
    ):
        if schema is not None:
            names = [str(s) for s in schema]
        elif rows and hasattr(rows[0], "asDict"):
            names = list(rows[0].asDict())
        elif rows and isinstance(rows[0], dict):
            # Spark infers a schema from a dict row with *sorted* keys.
            names = sorted(rows[0])
        else:
            width = len(rows[0]) if rows else 0
            names = [f"_{i + 1}" for i in range(width)]
        columns = [_row_values(r, names) for r in rows]
        types = [_infer_duck_type([c[i] for c in columns if i < len(c)]) for i in range(len(names))]
        return names, types
    fields = getattr(schema, "fields", None)
    if fields is None and isinstance(schema, str):
        from ovid2.core.engines.types import StructField, StructType, _as_type

        parsed = []
        for part in _split_ddl(schema):
            bits = part.strip().replace(":", " ", 1).split(None, 1)
            if not bits:
                continue
            parsed.append(
                StructField(bits[0].strip("`"), _as_type(bits[1] if len(bits) > 1 else "string"))
            )
        fields = StructType(parsed).fields
    if fields is not None:
        names = [f.name for f in fields]
        types = [_duck_type_for_spark(str(f.dataType.simpleString())) for f in fields]
        return names, types
    raise TypeError(f"Unsupported createDataFrame schema {schema!r}")


def _split_ddl(text: str) -> list[str]:
    """Split ``"a string, b decimal(10,2)"`` on top-level commas."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _duck_type_for_spark(spark_type: str) -> str:
    t = spark_type.strip().lower()
    if t.startswith("decimal"):
        return t.upper()
    return _SPARK_TYPE_TO_DUCK.get(t, "VARCHAR")


__all__ = ["DuckDBEngine", "DuckDBEngineSession", "transpile_statement"]
