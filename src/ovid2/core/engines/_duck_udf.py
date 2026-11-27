"""Python UDFs for the DuckDB engine (Spark ``udf`` / ``pandas_udf`` semantics).

A Spark Python UDF runs the user's function per row and converts the
return value to the declared type. To make code that uses them
(``udf(fn, StringType())``, the legacy ``cf.*_udf`` helpers, pandas UDFs)
produce the same values on DuckDB, a :class:`DuckUdf` registers the
function as a DuckDB scalar Python function and wraps the declared-type
conversion Spark applies:

* **Inputs** arrive as DuckDB hands them to Python: ``str`` for VARCHAR,
  ``datetime`` for TIMESTAMP, ``date`` for DATE, ``Decimal`` for DECIMAL,
  ``float`` for DOUBLE, ``int`` for integers — the same Python types
  Spark's row UDF receives.
* **Outputs** follow Spark's converters: a ``StringType`` UDF renders
  ``bool`` as ``true``/``false``, ``float`` with Java ``Double.toString``
  and any other object with ``str()``; numeric return types yield NULL
  when the value is not of a matching Python type (Spark's
  ``FloatType`` UDF returns NULL for an ``int``).
* A function that raises fails the query, exactly like Spark.

Each function is registered once per connection under a stable name;
cursors created from the root connection see it (DuckDB shares function
catalogs across cursors).

No ``import duckdb`` here: registration goes through the connection
object the engine passes in.
"""

from __future__ import annotations

import datetime as _dt
import math
import threading
from decimal import Decimal
from itertools import count as _sequence
from typing import Any

from ovid2.core.engines._duck_column import DuckColumn, to_column
from ovid2.core.engines.types import spark_type_name

__all__ = ["DuckUdf", "java_double_text", "python_value_text"]

_counter = _sequence(1)
_lock = threading.Lock()
#: ``(id(connection), lower-case SQL name) -> DuckUdf`` for ``register_as``.
#: Keyed on ``id`` like :attr:`DuckUdf._registered`; a closed connection's
#: id may be reused, which at worst skips a needed registration, so
#: :func:`forget_connection` drops a connection's entries when it closes.
_named: dict[tuple[int, str], DuckUdf] = {}

#: Distinct argument tuples a deterministic row UDF remembers (per UDF and
#: connection); the memo is dropped wholesale when full.
_ROW_MEMO_SIZE = 1 << 17


def forget_connection(conn: Any) -> None:
    """Drop the ``register_as`` bookkeeping for ``conn`` (call before closing it)."""
    key_id = id(conn)
    with _lock:
        for key in [k for k in _named if k[0] == key_id]:
            del _named[key]


#: Spark simple type -> (DuckDB return type, column kind).
_RETURN_TYPES: dict[str, tuple[str, str]] = {
    "string": ("VARCHAR", "str"),
    "float": ("FLOAT", "num"),
    "double": ("DOUBLE", "num"),
    "int": ("INTEGER", "num"),
    "bigint": ("BIGINT", "num"),
    "smallint": ("SMALLINT", "num"),
    "tinyint": ("TINYINT", "num"),
    "boolean": ("BOOLEAN", "bool"),
    "date": ("DATE", "date"),
    "timestamp": ("TIMESTAMP", "ts"),
}


def java_double_text(value: float) -> str:
    """Render ``value`` like Java ``Double.toString`` (Spark's string conversion).

    Java prints the shortest round-trip digits (as Python ``repr`` does)
    in plain notation for ``1e-3 <= |x| < 1e7`` and as ``d.dddE<n>``
    otherwise, always with at least one fractional digit.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    sign = "-" if value < 0 else ""
    digits, exponent = _shortest_digits(abs(value))
    if -3 <= exponent < 7:
        if exponent >= 0:
            int_part = digits[: exponent + 1].ljust(exponent + 1, "0")
            frac = digits[exponent + 1 :] or "0"
        else:
            int_part = "0"
            frac = "0" * (-exponent - 1) + digits
        return f"{sign}{int_part}.{frac}"
    return f"{sign}{digits[0]}.{digits[1:] or '0'}E{exponent}"


def _shortest_digits(mag: float) -> tuple[str, int]:
    """Return ``(significant digits, decimal exponent)`` of ``repr(mag)``."""
    text = repr(mag)
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    raw = int_part + frac_part
    stripped = raw.lstrip("0")
    exponent += len(int_part) - 1 - (len(raw) - len(stripped))
    return (stripped.rstrip("0") or "0"), exponent


def python_value_text(value: Any) -> str | None:
    """Return the text Spark's ``StringType`` UDF converter yields for ``value``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return java_double_text(value)
    if isinstance(value, str):
        return value
    return str(value)


def _converter(spark_type: str) -> Any:
    """Return the return-value converter for a Spark simple type."""
    if spark_type == "string":
        return python_value_text
    if spark_type in ("float", "double"):
        return lambda v: float(v) if isinstance(v, float) else None
    if spark_type in ("int", "bigint", "smallint", "tinyint"):
        return lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None
    if spark_type == "boolean":
        return lambda v: v if isinstance(v, bool) else None
    if spark_type == "date":
        return lambda v: (
            v.date() if isinstance(v, _dt.datetime) else (v if isinstance(v, _dt.date) else None)
        )
    if spark_type == "timestamp":
        return lambda v: v if isinstance(v, _dt.datetime) else None
    if spark_type.startswith("decimal"):
        return lambda v: v if isinstance(v, (Decimal, int)) and not isinstance(v, bool) else None
    return lambda v: v


def _normalise_input(value: Any) -> Any:
    """Match the Python value Spark's row UDF receives."""
    if isinstance(value, Decimal):
        return value
    return value


class DuckUdf:
    """A Spark-style UDF usable in DuckDB column expressions.

    Args:
        fn: The Python function (row-at-a-time semantics).
        return_type: Spark type (portable / PySpark / DDL text); default ``string``.
        vectorised: ``True`` for a pandas UDF (``fn`` takes / returns a
            ``pandas.Series``).
    """

    def __init__(self, fn: Any, return_type: Any = "string", *, vectorised: bool = False) -> None:
        self.func = fn
        self._vectorised = vectorised
        self._spark_type = spark_type_name(return_type or "string")
        if self._spark_type.startswith("decimal"):
            duck_type, kind = self._spark_type.upper(), "num"
        else:
            duck_type, kind = _RETURN_TYPES.get(self._spark_type, ("VARCHAR", "str"))
        self._duck_type = duck_type
        self._kind = kind
        self.returnType = return_type
        self._registered: dict[int, str] = {}
        self.__name__ = getattr(fn, "__name__", "udf")
        #: Spark treats a UDF as deterministic (it may dedupe / reorder
        #: calls) unless ``asNondeterministic()`` is called; only then are
        #: row results never memoised.
        self.deterministic = True

    # -- registration -------------------------------------------------------

    def _python(self) -> Any:
        fn, convert = self.func, _converter(self._spark_type)
        if self._vectorised:

            def _vector(*cols: Any) -> Any:
                import pandas as pd

                series = [pd.Series([_normalise_input(v) for v in c], dtype=object) for c in cols]
                out = fn(*series)
                return [convert(None if _is_na(v) else v) for v in list(out)]

            return _vector

        if not self.deterministic:

            def _row_uncached(*args: Any) -> Any:
                return convert(fn(*[_normalise_input(a) for a in args]))

            return _row_uncached

        memo: dict[tuple[Any, ...], Any] = {}

        def _row(*args: Any) -> Any:
            # Type-tagged key: 1 / 1.0 / True must not share a result.
            key = tuple((type(a), a) for a in args)
            try:
                return memo[key]
            except KeyError:
                pass
            except TypeError:  # unhashable argument (list / dict): no memo
                return convert(fn(*[_normalise_input(a) for a in args]))
            out = convert(fn(*[_normalise_input(a) for a in args]))
            if len(memo) >= _ROW_MEMO_SIZE:
                memo.clear()
            memo[key] = out
            return out

        return _row

    def sql_name(self, conn: Any) -> str:
        """Register on ``conn`` (once) and return the SQL function name."""
        key = id(conn)
        with _lock:
            name = self._registered.get(key)
            if name is not None:
                return name
            name = f"__ovid2_udf_{next(_counter)}"
            self._create(conn, name)
            self._registered[key] = name
            return name

    def register_as(self, conn: Any, name: str) -> None:
        """Register on ``conn`` under a caller-chosen SQL name (``udf.register``).

        Idempotent: re-registering the same UDF under the same name on the
        same connection is a no-op. That matters beyond speed: DuckDB
        (1.5) cannot re-create a removed function while the connection
        holds a partially consumed streaming result (an interrupted or
        abandoned ``to_arrow_batches``) — ``create_function`` then raises
        "already exists" for the rest of the session.
        """
        key = (id(conn), name.lower())
        with _lock:
            if _named.get(key) is self:
                return
            try:
                conn.remove_function(name)
            except Exception as exc:  # not registered yet
                del exc
            self._create(conn, name)
            _named[key] = self

    def _create(self, conn: Any, name: str) -> None:
        impl = self._python()
        if self._vectorised:
            conn.create_function(
                name,
                _vector_adapter(impl),
                None,
                self._duck_type,
                type="arrow",
                null_handling="special",
                side_effects=False,
            )
        else:
            conn.create_function(
                name, impl, None, self._duck_type, null_handling="special", side_effects=False
            )

    # -- call -----------------------------------------------------------------

    def __call__(self, *cols: Any) -> DuckColumn:
        """Return a column applying the UDF to ``cols`` (names or columns)."""
        from ovid2.core.engines.dsl import active_engine

        args = [to_column(c) for c in cols]
        udf = self

        def _render(ctx: Any) -> str:
            engine = active_engine()
            conn_fn = getattr(engine, "_root_connection", None)
            if conn_fn is None:
                raise RuntimeError("DuckDB UDF used without an active DuckDB engine")
            name = udf.sql_name(conn_fn())
            return f"{name}({', '.join(a.sql(ctx) for a in args)})"

        return DuckColumn(
            _render, self._kind, name=f"{self.__name__}({', '.join(_arg_name(c) for c in cols)})"
        )

    def asNondeterministic(self) -> DuckUdf:  # noqa: N802 — PySpark API
        """Spark API: never memoise this UDF's results (call it for every row).

        Must be called before the UDF is first used in a query (as in Spark,
        where it returns the UDF to use); later registrations honour it.
        """
        self.deterministic = False
        self._registered.clear()
        return self


def _arg_name(col: Any) -> str:
    if isinstance(col, str):
        return col
    name = getattr(col, "output_name", None)
    return str(name) if name else "col"


def _is_na(value: Any) -> bool:
    if value is None:
        return True
    try:
        return isinstance(value, float) and math.isnan(value)
    except TypeError:
        return False


def _vector_adapter(impl: Any) -> Any:
    """Wrap a list-in / list-out function as a DuckDB Arrow UDF.

    A single-argument chunk is dictionary-encoded first and ``impl`` runs
    on the *distinct* values only, then the results are expanded back
    with ``take`` — a pandas UDF is a pure function of its input values
    (Spark gives no row identity to it), so this is exact.
    """

    def _arrow(*arrays: Any) -> Any:
        import pyarrow as pa

        if len(arrays) == 1 and len(arrays[0]) > 1:
            arr = arrays[0]
            if isinstance(arr, pa.ChunkedArray):
                arr = arr.combine_chunks()
            try:
                encoded = arr.dictionary_encode(null_encoding="encode")
            except (pa.ArrowNotImplementedError, pa.ArrowInvalid, TypeError):
                encoded = None
            if encoded is not None and len(encoded.dictionary) < len(arr):
                distinct = pa.array(impl(encoded.dictionary.to_pylist()))
                # Expand back: a dictionary array over the per-value results,
                # decoded to a plain array (no per-row Python).
                expanded = pa.DictionaryArray.from_arrays(encoded.indices, distinct)
                return expanded.dictionary_decode()
        cols = [a.to_pylist() for a in arrays]
        return pa.array(impl(*cols))

    return _arrow


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
