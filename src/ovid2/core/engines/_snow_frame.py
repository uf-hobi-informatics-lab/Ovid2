"""PySpark-shaped DataFrame / Column / functions over Snowpark (the Snowpark engine).

Shared Ovid2 code — fixers, mapper ops, transforms, formatter adapters,
generated legacy ports and vendored legacy fix scripts — is written
against the PySpark surface (:mod:`ovid2.core.engines.surface`). Snowpark
is close to it but not identical (``when`` chains, ``ilike``,
``bitwiseAND``, ``cache``, ``rdd``, ``udf`` signatures, ``F.first`` …).
This module wraps native Snowpark objects so that surface works
unchanged:

* :class:`SnowFrame` — wraps ``snowflake.snowpark.DataFrame``; every
  method returns a :class:`SnowFrame`; unknown attributes fall through
  to the native frame (Snowpark mirrors most PySpark names).
* :class:`SnowColumn` — wraps ``snowflake.snowpark.Column``; operators
  and methods return :class:`SnowColumn`.
* :class:`SnowFunctions` — the full :data:`~ovid2.core.engines.surface.FUNCTIONS`
  set, mapping each name to Snowflake SQL with Spark semantics
  (lenient casts, ``TRY_*`` date parsing, Java date patterns).
* UDFs — ``udf`` / ``pandas_udf`` register temporary Snowflake Python
  UDFs from the function (``cloudpickle``; ``python-dateutil`` /
  ``pandas`` packages), with Spark's return-type conversion.

Only :mod:`ovid2.core.engines.snowpark` constructs these.
"""

from __future__ import annotations

import contextlib
import re
from itertools import count as _sequence
from typing import Any, cast

__all__ = ["SnowColumn", "SnowFrame", "SnowFunctions", "unwrap", "wrap_column"]

_udf_counter = _sequence(1)


def unwrap(value: Any) -> Any:
    """Return the native Snowpark object behind a wrapper (or ``value``)."""
    if isinstance(value, (SnowColumn, SnowFrame)):
        return value.native
    if isinstance(value, (list, tuple)):
        return type(value)(unwrap(v) for v in value)
    return value


def wrap_column(value: Any) -> SnowColumn:
    """Wrap a native column / literal / name as a :class:`SnowColumn`."""
    from snowflake.snowpark import Column
    from snowflake.snowpark import functions as F

    if isinstance(value, SnowColumn):
        return value
    if isinstance(value, Column):
        return SnowColumn(value)
    if isinstance(value, str):
        return SnowColumn(F.col(_quote_name(value)))
    return SnowColumn(F.lit(value))


def _quote_name(name: str) -> str:
    """Spark column names are case-insensitive; Snowpark folds unquoted names to upper.

    Names with characters outside ``[A-Za-z0-9_]`` (``"a b"``, ``"x.y"``
    back-quoted Spark style) are passed as quoted identifiers.
    """
    if name.startswith("`") and name.endswith("`"):
        name = name[1:-1].replace("``", "`")
        if name.replace("_", "").isalnum() and not name[:1].isdigit():
            # A back-quoted plain name is still case-insensitive in Spark.
            return name
        return '"' + name.replace('"', '""') + '"'
    if name == "*" or name.replace("_", "").isalnum():
        return name
    if "." in name:
        return name  # ``alias.col`` — Snowpark resolves qualified refs itself
    return '"' + name.replace('"', '""') + '"'


def _native_col(name: str) -> Any:
    """Return ``snowflake.snowpark.functions.col`` for a Spark column reference.

    Spark resolves ``"alias.column"`` against an aliased frame
    (``df.alias("a")``); Snowpark needs the two-part form
    ``col("a", "column")``. A dotted name whose parts are plain identifiers is
    split that way; anything else keeps ``_quote_name``'s rules.
    """
    from snowflake.snowpark import functions as F

    if "." in name and not name.startswith("`"):
        qualifier, _, column = name.partition(".")
        if _PLAIN_IDENTIFIER.fullmatch(qualifier) and _PLAIN_IDENTIFIER.fullmatch(column):
            return F.col(_snow_alias(qualifier), _quote_name(column))
    return F.col(_quote_name(name))


def _snow_alias(name: str) -> str:
    """Snowpark frame alias for a Spark alias (Spark aliases are case-insensitive)."""
    return name.upper()


def _arg(value: Any, *, literal_strings: bool = False) -> Any:
    """Native column for a function argument (names → col unless literal)."""
    from snowflake.snowpark import functions as F

    if isinstance(value, SnowColumn):
        return value.native
    if isinstance(value, str) and not literal_strings:
        return _native_col(value)
    from snowflake.snowpark import Column

    if isinstance(value, Column):
        return value
    return F.lit(value)


class SnowColumn:
    """PySpark-shaped wrapper over ``snowflake.snowpark.Column``."""

    __slots__ = ("_case", "native")

    def __init__(self, native: Any, case: list[tuple[Any, Any]] | None = None) -> None:
        self.native = native
        self._case = case

    # -- naming / casting ---------------------------------------------------

    def alias(self, *names: str, **_kw: Any) -> SnowColumn:
        aliased = self.native.alias(_quote_name(names[0]))
        if id(self.native) in _EXPLODED:
            _EXPLODED.add(id(aliased))
        return SnowColumn(aliased)

    name = alias

    def cast(self, data_type: Any) -> SnowColumn:
        from ovid2.core.engines.types import spark_type_name

        return SnowColumn(_spark_cast(self.native, spark_type_name(data_type)))

    astype = cast

    # -- predicates ---------------------------------------------------------

    def isNull(self) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self.native.is_null())

    def isNotNull(self) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self.native.is_not_null())

    def isin(self, *values: Any) -> SnowColumn:
        if len(values) == 1 and isinstance(values[0], (list, tuple, set, frozenset)):
            values = tuple(values[0])
        if not values:
            from snowflake.snowpark import functions as F

            return SnowColumn(F.lit(False))
        return SnowColumn(self.native.isin([_arg(v, literal_strings=True) for v in values]))

    def rlike(self, pattern: str) -> SnowColumn:
        from snowflake.snowpark import functions as F

        if _needs_python_regex(pattern):
            return _python_regex_udf("search", pattern)(self)
        # Spark RLIKE is a *search*; Snowflake ``REGEXP`` anchors the whole value.
        return SnowColumn(F.builtin("REGEXP_INSTR")(self.native, F.lit(pattern)) > 0)

    def like(self, pattern: str) -> SnowColumn:
        return SnowColumn(self.native.like(pattern))

    def ilike(self, pattern: str) -> SnowColumn:
        from snowflake.snowpark import functions as F

        return SnowColumn(F.builtin("ILIKE")(self.native, F.lit(pattern)))

    def startswith(self, other: Any) -> SnowColumn:
        return SnowColumn(self.native.startswith(_arg(other, literal_strings=True)))

    def endswith(self, other: Any) -> SnowColumn:
        return SnowColumn(self.native.endswith(_arg(other, literal_strings=True)))

    def contains(self, other: Any) -> SnowColumn:
        return SnowColumn(self.native.contains(_arg(other, literal_strings=True)))

    def substr(self, start: int, length: int) -> SnowColumn:
        return SnowColumn(self.native.substr(1 if start == 0 else start, length))

    def between(self, low: Any, high: Any) -> SnowColumn:
        return SnowColumn(
            self.native.between(_arg(low, literal_strings=True), _arg(high, literal_strings=True))
        )

    def eqNullSafe(self, other: Any) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self.native.equal_null(_arg(other, literal_strings=True)))

    def bitwiseAND(self, other: Any) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self.native.bitand(_arg(other, literal_strings=True)))

    def getItem(self, key: Any) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self.native[key])

    def __getitem__(self, key: Any) -> SnowColumn:
        return self.getItem(key)

    def over(self, window: Any) -> SnowColumn:
        spec = window.native if isinstance(window, SnowWindowSpec) else unwrap(window)
        return SnowColumn(self.native.over(spec))

    # -- ordering -----------------------------------------------------------

    def asc(self) -> SnowColumn:
        return SnowColumn(self.native.asc_nulls_first())

    def desc(self) -> SnowColumn:
        return SnowColumn(self.native.desc_nulls_last())

    def asc_nulls_last(self) -> SnowColumn:
        return SnowColumn(self.native.asc_nulls_last())

    def asc_nulls_first(self) -> SnowColumn:
        return SnowColumn(self.native.asc_nulls_first())

    def desc_nulls_first(self) -> SnowColumn:
        return SnowColumn(self.native.desc_nulls_first())

    def desc_nulls_last(self) -> SnowColumn:
        return SnowColumn(self.native.desc_nulls_last())

    # -- case ---------------------------------------------------------------

    def when(self, condition: Any, value: Any) -> SnowColumn:
        return SnowColumn(self.native.when(_arg(condition), _arg(value, literal_strings=True)))

    def otherwise(self, value: Any) -> SnowColumn:
        return SnowColumn(self.native.otherwise(_arg(value, literal_strings=True)))

    # -- operators ----------------------------------------------------------

    _COMPARE = frozenset({"__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"})
    _ARITH = frozenset({"__add__", "__sub__", "__mul__", "__truediv__", "__mod__"})

    def _bin(self, op: str, other: Any, reverse: bool = False) -> SnowColumn:
        from snowflake.snowpark import functions as F

        mine = self.native
        o = _arg(other, literal_strings=True)
        numeric_other = isinstance(other, (int, float)) and not isinstance(other, bool)
        if op in self._ARITH or (op in self._COMPARE and numeric_other):
            # Spark (ANSI off): a string operand is cast to double, NULL on garbage.
            mine = F.builtin("TRY_TO_DOUBLE")(mine.cast("string"))
            if op in self._ARITH and not numeric_other:
                o = F.builtin("TRY_TO_DOUBLE")(o.cast("string"))
        a, b = (o, mine) if reverse else (mine, o)
        return SnowColumn(getattr(a, op)(b))

    def __eq__(self, other: Any) -> SnowColumn:  # type: ignore[override]
        return self._bin("__eq__", other)

    def __ne__(self, other: Any) -> SnowColumn:  # type: ignore[override]
        return self._bin("__ne__", other)

    def __lt__(self, other: Any) -> SnowColumn:
        return self._bin("__lt__", other)

    def __le__(self, other: Any) -> SnowColumn:
        return self._bin("__le__", other)

    def __gt__(self, other: Any) -> SnowColumn:
        return self._bin("__gt__", other)

    def __ge__(self, other: Any) -> SnowColumn:
        return self._bin("__ge__", other)

    def __and__(self, other: Any) -> SnowColumn:
        return self._bin("__and__", other)

    def __or__(self, other: Any) -> SnowColumn:
        return self._bin("__or__", other)

    def __rand__(self, other: Any) -> SnowColumn:
        return self._bin("__and__", other, reverse=True)

    def __ror__(self, other: Any) -> SnowColumn:
        return self._bin("__or__", other, reverse=True)

    def __invert__(self) -> SnowColumn:
        return SnowColumn(~self.native)

    def __neg__(self) -> SnowColumn:
        return SnowColumn(-self.native)

    def __add__(self, other: Any) -> SnowColumn:
        return self._bin("__add__", other)

    def __radd__(self, other: Any) -> SnowColumn:
        return self._bin("__add__", other, reverse=True)

    def __sub__(self, other: Any) -> SnowColumn:
        return self._bin("__sub__", other)

    def __rsub__(self, other: Any) -> SnowColumn:
        return self._bin("__sub__", other, reverse=True)

    def __mul__(self, other: Any) -> SnowColumn:
        return self._bin("__mul__", other)

    def __rmul__(self, other: Any) -> SnowColumn:
        return self._bin("__mul__", other, reverse=True)

    def __truediv__(self, other: Any) -> SnowColumn:
        return self._bin("__truediv__", other)

    def __rtruediv__(self, other: Any) -> SnowColumn:
        return self._bin("__truediv__", other, reverse=True)

    def __mod__(self, other: Any) -> SnowColumn:
        return self._bin("__mod__", other)

    def __bool__(self) -> bool:
        raise ValueError(
            "Cannot convert a column into bool: use '&' for 'and', '|' for 'or', '~' for 'not'."
        )

    __hash__ = object.__hash__

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.native, name)
        if callable(attr):
            return lambda *a, **k: _wrap_result(attr(*[unwrap(x) for x in a], **k))
        return attr

    def __repr__(self) -> str:
        return f"SnowColumn<{self.native}>"


def _wrap_result(value: Any) -> Any:
    from snowflake.snowpark import Column, DataFrame

    if isinstance(value, Column):
        return SnowColumn(value)
    if isinstance(value, DataFrame):
        return SnowFrame(value)
    return value


def _float_type_of(native: Any) -> bool | None:
    """``True`` if ``native`` is a FLOAT/DOUBLE column, ``None`` when unknown."""
    expression = getattr(native, "_expression", None)
    datatype = getattr(expression, "datatype", None)
    if datatype is None:
        return None
    return type(datatype).__name__ in ("DoubleType", "FloatType")


def _spark_string_cast(native: Any) -> Any:
    """``CAST(x AS STRING)`` with Spark's rendering of doubles.

    Snowflake prints ``56::DOUBLE`` as ``56`` and keeps ~15 significant
    digits; Spark prints Java's ``Double.toString`` (``56.0``, shortest
    round-trip digits, ``1.0E20``). For a double column the cast goes
    through a Python UDF that applies Spark's rules exactly; any other
    type keeps the native cast.
    """
    known = _float_type_of(native)
    if known is False:
        return native.cast("string")
    udf = _double_text_udf()
    if udf is None:
        return native.cast("string")
    if known:
        return udf(native)
    # Static type unknown (an unresolved ``col``): decide per value.
    # ``AS_DOUBLE`` never raises, so the branch is safe for any type.
    from snowflake.snowpark import functions as F

    variant = F.to_variant(native)
    return F.when(
        F.builtin("TYPEOF")(variant) == F.lit("DOUBLE"),
        udf(F.builtin("AS_DOUBLE")(variant)),
    ).otherwise(native.cast("string"))


_DOUBLE_TEXT_UDFS: dict[int, tuple[Any, Any]] = {}


def _double_text_udf() -> Any:
    """Per-session temporary ``DOUBLE -> VARCHAR`` UDF (Java ``Double.toString``)."""
    from snowflake.snowpark import types as T
    from snowflake.snowpark.context import get_active_session

    try:
        session = get_active_session()
    except Exception:  # no active session: fall back to the native cast
        return None
    hit = _DOUBLE_TEXT_UDFS.get(id(session))
    if hit is not None and hit[0] is session:
        return hit[1]
    from ovid2.core.engines._duck_udf import java_double_text

    def _render(value: float | None) -> str | None:
        return None if value is None else java_double_text(value)

    # ``java_double_text`` (and its module) travel inside the pickle.
    _pickle_ovid2_by_value(_render)

    udf = session.udf.register(
        _render,
        return_type=T.StringType(),
        input_types=[T.DoubleType()],
        name=f"OVID2_DOUBLE_TEXT_{next(_udf_counter)}",
        is_permanent=False,
        replace=True,
    )
    _DOUBLE_TEXT_UDFS[id(session)] = (session, udf)
    return udf


def _spark_cast(native: Any, spark_type: str) -> Any:
    """Spark (ANSI off) cast: NULL on bad input, integral truncation."""
    from snowflake.snowpark import functions as F

    t = spark_type.lower()
    text = native.cast("string")
    if t in ("string", "varchar", "void"):
        return _spark_string_cast(native)
    if t in ("int", "integer", "bigint", "long", "smallint", "tinyint", "short", "byte"):
        return F.builtin("TRUNCATE")(F.builtin("TRY_TO_DOUBLE")(F.trim(text))).cast("bigint")
    if t in ("double", "float", "real"):
        return F.builtin("TRY_TO_DOUBLE")(F.trim(text))
    if t.startswith("decimal"):
        inner = t[len("decimal") :].strip("() ")
        p, _, s = inner.partition(",")
        return F.builtin("TRY_TO_DECIMAL")(F.trim(text), F.lit(int(p or 10)), F.lit(int(s or 0)))
    if t == "boolean":
        return F.builtin("TRY_TO_BOOLEAN")(F.trim(text))
    if t == "date":
        return F.builtin("TRY_TO_DATE")(F.trim(text))
    if t.startswith("timestamp"):
        return F.builtin("TRY_TO_TIMESTAMP_NTZ")(F.trim(text))
    return native.cast(t)


class _SnowGrouped:
    """``GroupedData`` over Snowpark's ``RelationalGroupedDataFrame``."""

    def __init__(self, native: Any, *, pivoted: bool = False) -> None:
        self._native = native
        self._pivoted = pivoted

    def agg(self, *exprs: Any) -> SnowFrame:
        if len(exprs) == 1 and isinstance(exprs[0], dict):
            from snowflake.snowpark import functions as F

            # Spark names a dict aggregate ``fn(col)``; after ``pivot`` the
            # output columns are the pivot values instead, and an alias on
            # the aggregate makes Snowflake reject the PIVOT
            # (``"values".X is not a valid group by expression`` — found by
            # the algorithm conformance matrix).
            cols = [
                getattr(F, fn)(F.col(_quote_name(c)))
                if self._pivoted
                else getattr(F, fn)(F.col(_quote_name(c))).alias(f'"{fn}({c})"')
                for c, fn in exprs[0].items()
            ]
            return SnowFrame(_unquote_pivot_columns(self._native.agg(*cols), self._pivoted))
        return SnowFrame(self._native.agg(*[unwrap(e) for e in exprs]))

    def count(self) -> SnowFrame:
        from snowflake.snowpark import functions as F

        return SnowFrame(self._native.agg(F.builtin("COUNT")(F.lit(1)).alias('"count"')))

    def _simple(self, fn: str, cols: tuple[str, ...]) -> SnowFrame:
        from snowflake.snowpark import functions as F

        return SnowFrame(self._native.agg(*[getattr(F, fn)(F.col(_quote_name(c))) for c in cols]))

    def max(self, *cols: str) -> SnowFrame:
        return self._simple("max", cols)

    def min(self, *cols: str) -> SnowFrame:
        return self._simple("min", cols)

    def sum(self, *cols: str) -> SnowFrame:
        return self._simple("sum", cols)

    def avg(self, *cols: str) -> SnowFrame:
        return self._simple("avg", cols)

    mean = avg

    def pivot(self, pivot_col: Any, values: list[Any] | None = None) -> _SnowGrouped:
        return _SnowGrouped(self._native.pivot(_arg(pivot_col), values), pivoted=True)


def _unquote_pivot_columns(native: Any, pivoted: bool) -> Any:
    """Rename Snowflake's ``'a'`` pivot columns to Spark's bare ``a``."""
    if not pivoted:
        return native
    from snowflake.snowpark import functions as F

    projection = []
    for c in native.columns:
        # Snowpark reports pivot outputs as the quoted identifier ``"'a'"``.
        inner = c[1:-1] if len(c) >= 2 and c[0] == '"' and c[-1] == '"' else c
        if len(inner) >= 2 and inner[0] == "'" and inner[-1] == "'":
            value = inner[1:-1]
            projection.append(F.col(c).alias(_quote_name(value) if value else '""'))
        else:
            projection.append(F.col(c))
    return native.select(projection)


class SnowFrame:
    """PySpark-shaped wrapper over ``snowflake.snowpark.DataFrame``."""

    __slots__ = ("_engine", "native")

    def __init__(self, native: Any, engine: Any = None) -> None:
        object.__setattr__(self, "native", native)
        object.__setattr__(self, "_engine", engine)

    def _w(self, native: Any) -> SnowFrame:
        return SnowFrame(native, self._engine)

    # -- introspection ------------------------------------------------------

    @property
    def columns(self) -> list[str]:
        return [_spark_name(c) for c in self.native.columns]

    @property
    def dtypes(self) -> list[tuple[str, str]]:
        return [(_spark_name(n), str(t)) for n, t in self.native.dtypes]

    @property
    def schema(self) -> Any:
        from ovid2.core.engines.types import StructField, StructType, _as_type

        return StructType(
            [
                StructField(
                    _spark_name(f.name), _as_type(_snow_to_spark(type(f.datatype).__name__))
                )
                for f in self.native.schema.fields
            ]
        )

    @property
    def sparkSession(self) -> Any:  # noqa: N802 — PySpark API
        if self._engine is not None:
            return self._engine.get_session()
        return self.native.session

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return SnowColumn(self.native[_quote_name(item)])
        if isinstance(item, int):
            return SnowColumn(self.native[self.native.columns[item]])
        if isinstance(item, SnowColumn):
            return self.filter(item)
        if isinstance(item, (list, tuple)):
            return self.select(*item)
        raise TypeError(f"Unsupported index {item!r}")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name.lower() in {c.lower() for c in self.columns}:
            return SnowColumn(self.native[_quote_name(name)])
        attr = getattr(self.native, name)
        if callable(attr):
            return lambda *a, **k: _wrap_frame_result(
                attr(*[unwrap(x) for x in a], **k), self._engine
            )
        return attr

    # -- projection ---------------------------------------------------------

    def _cols(self, cols: tuple[Any, ...]) -> list[Any]:
        out: list[Any] = []
        for c in cols:
            if isinstance(c, (list, tuple)):
                out.extend(self._cols(tuple(c)))
            else:
                out.append(_arg(c))
        return out

    def select(self, *cols: Any) -> SnowFrame:
        natives = self._cols(cols)
        exploded = [
            i for i, c in enumerate(natives) if id(_unaliased(c)) in _EXPLODED or id(c) in _EXPLODED
        ]
        out = self.native.select(*natives)
        if not exploded:
            return self._w(out)
        from snowflake.snowpark import functions as F

        names = list(out.columns)
        out = out.select(
            *[
                F.col(n).cast("string").alias(n) if i in exploded else F.col(n)
                for i, n in enumerate(names)
            ]
        )
        return self._w(out)

    def selectExpr(self, *exprs: str) -> SnowFrame:  # noqa: N802 — PySpark API
        flat: list[str] = []
        for e in exprs:
            flat.extend(e if isinstance(e, (list, tuple)) else [e])
        return self._w(self.native.select_expr(*[_spark_sql(e) for e in flat]))

    def withColumn(self, name: str, col: Any) -> SnowFrame:  # noqa: N802 — PySpark API
        value = _arg(col, literal_strings=True)
        bare = (
            name[1:-1].replace("``", "`") if name.startswith("`") and name.endswith("`") else name
        )
        natives = list(self.native.columns)
        hit = next(
            (i for i, c in enumerate(natives) if _spark_name(c).lower() == bare.lower()), None
        )
        if hit is None:
            return self._w(self.native.with_column(_quote_name(name), value))
        # Spark replaces an existing column *in place*; Snowpark's
        # ``with_column`` drops it and appends the new one at the end.
        from snowflake.snowpark import functions as F

        projection = [
            value.alias(natives[i]) if i == hit else F.col(natives[i]) for i in range(len(natives))
        ]
        return self._w(self.native.select(projection))

    def withColumns(self, mapping: dict[str, Any]) -> SnowFrame:  # noqa: N802 — PySpark API
        frame = self
        for k, v in mapping.items():
            frame = frame.withColumn(k, v)
        return frame

    def withColumnRenamed(self, existing: str, new: str) -> SnowFrame:  # noqa: N802
        if existing.lower() not in {c.lower() for c in self.columns}:
            return self
        return self._w(self.native.with_column_renamed(_quote_name(existing), _quote_name(new)))

    def toDF(self, *names: str) -> SnowFrame:  # noqa: N802 — PySpark API
        if len(names) == 1 and isinstance(names[0], (list, tuple)):
            names = tuple(names[0])
        return self._w(self.native.to_df(*[_quote_name(n) for n in names]))

    def drop(self, *cols: Any) -> SnowFrame:
        names = []
        lowered = {c.lower(): c for c in self.columns}
        for c in cols:
            if isinstance(c, SnowColumn):
                names.append(c.native)
            elif isinstance(c, str) and c.lower() in lowered:
                names.append(_quote_name(lowered[c.lower()]))
        return self._w(self.native.drop(*names)) if names else self

    def alias(self, name: str) -> SnowFrame:
        return self._w(self.native.alias(_snow_alias(name)))

    # -- rows ---------------------------------------------------------------

    def filter(self, condition: Any) -> SnowFrame:
        if isinstance(condition, str):
            from snowflake.snowpark import functions as F

            del F
            return self._w(self.native.filter(_sql_column(condition)))
        return self._w(self.native.filter(_arg(condition)))

    where = filter

    def distinct(self) -> SnowFrame:
        return self._w(self.native.distinct())

    def dropDuplicates(self, subset: Any = None) -> SnowFrame:  # noqa: N802 — PySpark API
        if not subset:
            return self._w(self.native.distinct())
        return self._w(self.native.drop_duplicates(*[_quote_name(s) for s in subset]))

    drop_duplicates = dropDuplicates

    def limit(self, n: int) -> SnowFrame:
        return self._w(self.native.limit(int(n)))

    def orderBy(self, *cols: Any, ascending: Any = None) -> SnowFrame:  # noqa: N802
        columns = [wrap_column(c) for c in self._flat(cols)]
        if ascending is not None:
            flags = (
                ascending if isinstance(ascending, (list, tuple)) else [ascending] * len(columns)
            )
            columns = [c.asc() if f else c.desc() for c, f in zip(columns, flags, strict=True)]
        return self._w(self.native.sort(*[c.native for c in columns]))

    sort = orderBy

    @staticmethod
    def _flat(cols: tuple[Any, ...]) -> list[Any]:
        out: list[Any] = []
        for c in cols:
            out.extend(c if isinstance(c, (list, tuple)) else [c])
        return out

    def union(self, other: SnowFrame) -> SnowFrame:
        return self._w(self.native.union_all(unwrap(other)))

    unionAll = union  # noqa: N815 — PySpark API

    def unionByName(self, other: SnowFrame, allowMissingColumns: bool = False) -> SnowFrame:  # noqa: N802, N803
        left, right = self.native, unwrap(other)
        if allowMissingColumns:
            from snowflake.snowpark import functions as F

            lcols, rcols = set(left.columns), set(right.columns)
            for c in right.columns:
                if c not in lcols:
                    left = left.with_column(c, F.lit(None).cast("string"))
            for c in left.columns:
                if c not in rcols:
                    right = right.with_column(c, F.lit(None).cast("string"))
        return self._w(left.union_all_by_name(right))

    def subtract(self, other: SnowFrame) -> SnowFrame:
        return self._w(self.native.except_(unwrap(other)))

    def exceptAll(self, other: SnowFrame) -> SnowFrame:  # noqa: N802 — PySpark API
        from snowflake.snowpark import Window
        from snowflake.snowpark import functions as F

        cols = self.native.columns
        w = Window.partition_by(*cols).order_by(*cols)
        left = self.native.with_column("__OVID2_DUP", F.row_number().over(w))
        right = unwrap(other).with_column("__OVID2_DUP", F.row_number().over(w))
        return self._w(left.except_(right).drop("__OVID2_DUP"))

    def intersect(self, other: SnowFrame) -> SnowFrame:
        return self._w(self.native.intersect(unwrap(other)))

    # -- joins --------------------------------------------------------------

    def join(self, other: SnowFrame, on: Any = None, how: str | None = None) -> SnowFrame:
        how_norm = (how or "inner").lower().replace("_", "")
        how_norm = {
            "leftsemi": "semi",
            "leftanti": "anti",
            "outer": "full",
            "fullouter": "full",
            "leftouter": "left",
            "rightouter": "right",
        }.get(how_norm, how_norm)
        right = unwrap(other)
        if on is None:
            return self._w(self.native.cross_join(right))
        if isinstance(on, str) and not any(ch in on for ch in "=<> ("):
            on = [on]
        if isinstance(on, (list, tuple)) and all(isinstance(x, str) for x in on):
            return self._w(
                self.native.join(right, [_quote_name(x) for x in on], join_type=how_norm)
            )
        if isinstance(on, (list, tuple)):
            cond = _arg(on[0])
            for p in on[1:]:
                cond = cond & _arg(p)
        elif isinstance(on, str):
            from snowflake.snowpark import functions as F

            del F
            cond = _sql_column(on)
        else:
            cond = _arg(on)
        return self._w(self.native.join(right, cond, join_type=how_norm))

    def crossJoin(self, other: SnowFrame) -> SnowFrame:  # noqa: N802 — PySpark API
        return self._w(self.native.cross_join(unwrap(other)))

    # -- aggregation --------------------------------------------------------

    def groupBy(self, *cols: Any) -> _SnowGrouped:  # noqa: N802 — PySpark API
        return _SnowGrouped(self.native.group_by(*self._cols(cols)))

    groupby = groupBy

    def agg(self, *exprs: Any) -> SnowFrame:
        return _SnowGrouped(self.native.group_by()).agg(*exprs)

    # -- missing data -------------------------------------------------------

    def fillna(self, value: Any, subset: Any = None) -> SnowFrame:
        if isinstance(subset, str):
            subset = [subset]
        return self._w(self.native.fillna(value, subset=subset))

    def dropna(self, how: str = "any", thresh: int | None = None, subset: Any = None) -> SnowFrame:
        if isinstance(subset, str):
            subset = [subset]
        return self._w(self.native.dropna(how=how, thresh=thresh, subset=subset))

    def replace(self, to_replace: Any, value: Any = None, subset: Any = None) -> SnowFrame:
        if isinstance(subset, str):
            subset = [subset]
        return self._w(self.native.replace(to_replace, value, subset=subset))

    @property
    def na(self) -> Any:
        frame = self

        class _Na:
            def fill(self, value: Any, subset: Any = None) -> SnowFrame:
                return frame.fillna(value, subset)

            def drop(
                self, how: str = "any", thresh: int | None = None, subset: Any = None
            ) -> SnowFrame:
                return frame.dropna(how, thresh, subset)

            def replace(self, to_replace: Any, value: Any = None, subset: Any = None) -> SnowFrame:
                return frame.replace(to_replace, value, subset)

        return _Na()

    # -- execution / caching ------------------------------------------------

    def cache(self) -> SnowFrame:
        return self._w(self.native.cache_result())

    def persist(self, *_args: Any, **_kwargs: Any) -> SnowFrame:
        return self.cache()

    def checkpoint(self, eager: bool = True) -> SnowFrame:
        del eager
        return self.cache()

    localCheckpoint = checkpoint  # noqa: N815 — PySpark API

    def unpersist(self, blocking: bool = False) -> SnowFrame:
        del blocking
        return self

    def hint(self, *_a: Any, **_k: Any) -> SnowFrame:
        return self

    def repartition(self, *_a: Any, **_k: Any) -> SnowFrame:
        return self

    def coalesce(self, *_a: Any) -> SnowFrame:
        return self

    def transform(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(self, *args, **kwargs)

    def createOrReplaceTempView(self, name: str) -> None:  # noqa: N802 — PySpark API
        self.native.create_or_replace_temp_view(name)
        if self._engine is not None:
            names = self._engine.__dict__.setdefault("_temp_view_names", set())
            names.add(str(name).lower())

    createTempView = createOrReplaceTempView  # noqa: N815 — PySpark API
    create_or_replace_temp_view = createOrReplaceTempView

    def sample(
        self,
        withReplacement: bool | None = None,  # noqa: N803 — PySpark API
        fraction: float | None = None,
        seed: int | None = None,
    ) -> SnowFrame:
        if isinstance(withReplacement, float) and fraction is None:
            fraction, withReplacement = withReplacement, False  # noqa: N806
        del seed
        return self._w(self.native.sample(frac=float(fraction or 0.0)))

    def sampleBy(self, col: Any, fractions: dict[Any, float], seed: int | None = None) -> SnowFrame:  # noqa: N802
        return self._w(self.native.sample_by(_arg(col), fractions))

    @property
    def stat(self) -> Any:
        frame = self

        class _Stat:
            def sampleBy(  # noqa: N802
                self, col: Any, fractions: dict[Any, float], seed: int | None = None
            ) -> SnowFrame:
                return frame.sampleBy(col, fractions, seed)

        return _Stat()

    # -- actions ------------------------------------------------------------
    # Executed through ``to_local_iterator`` (the native eager path) so the
    # PySpark action names stay the protocol surface only.

    def _fetch_rows(self, limit: int | None = None) -> list[Any]:
        from ovid2.core.engines.types import Row

        names = self.columns
        native = self.native.limit(limit) if limit is not None else self.native
        return [Row(**dict(zip(names, tuple(r), strict=True))) for r in native.to_local_iterator()]

    def collect(self) -> list[Any]:
        return self._fetch_rows()

    def count(self) -> int:
        from snowflake.snowpark import functions as F

        rows = list(self.native.agg(F.builtin("COUNT")(F.lit(1))).to_local_iterator())
        return int(rows[0][0]) if rows else 0

    def first(self) -> Any:
        rows = self._fetch_rows(1)
        return rows[0] if rows else None

    head = first

    def take(self, n: int) -> list[Any]:
        return self._fetch_rows(int(n))

    def isEmpty(self) -> bool:  # noqa: N802 — PySpark API
        return not self._fetch_rows(1)

    def toPandas(self) -> Any:  # noqa: N802 — PySpark API
        return self.native.to_pandas()

    def show(self, n: int = 20, truncate: Any = True, vertical: bool = False) -> None:
        del truncate, vertical
        print(self.native.limit(n).to_pandas())  # noqa: T201 — PySpark API prints by contract

    def explain(self, *_a: Any) -> None:
        self.native.explain()

    @property
    def rdd(self) -> Any:
        from ovid2.core.engines._duck_frame import _DuckRdd

        return _DuckRdd(self._fetch_rows)

    @property
    def write(self) -> Any:
        return self.native.write

    def to_arrow_batches(self, batch_size: int = 0) -> Any:
        import pyarrow as pa

        del batch_size
        for part in self.native.to_arrow_batches():
            yield from (part.to_batches() if isinstance(part, pa.Table) else [part])

    def __repr__(self) -> str:
        return f"SnowFrame[{', '.join(self.columns)}]"


def _wrap_frame_result(value: Any, engine: Any) -> Any:
    from snowflake.snowpark import Column, DataFrame

    if isinstance(value, DataFrame):
        return SnowFrame(value, engine)
    if isinstance(value, Column):
        return SnowColumn(value)
    return value


def _spark_name(name: str) -> str:
    """Snowpark quotes non-upper identifiers (``"lower"``); Spark code sees ``lower``."""
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name


def _snow_to_spark(type_name: str) -> str:
    return {
        "StringType": "string",
        "LongType": "bigint",
        "IntegerType": "int",
        "ShortType": "smallint",
        "ByteType": "tinyint",
        "DoubleType": "double",
        "FloatType": "float",
        "BooleanType": "boolean",
        "DateType": "date",
        "TimestampType": "timestamp",
        "DecimalType": "decimal(38,0)",
        "BinaryType": "binary",
    }.get(type_name, "string")


_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _sql_column(text: str) -> Any:
    """Return a native Snowpark column for a Spark SQL expression string.

    ``sql_expr`` text cannot reference an aliased frame (``a.code`` fails
    with *invalid identifier*: Snowpark resolves frame aliases only through
    ``col("a", "code")``). An expression that *does* qualify columns with an
    alias and is a boolean combination (AND / OR / NOT) of comparisons
    between such references, literals and ``upper`` / ``lower`` / ``trim``
    calls — the shape the join adapters build — is converted to Snowpark
    column objects. Everything else goes through ``sql_expr`` unchanged.
    """
    from snowflake.snowpark import functions as F

    explode = _EXPLODE_CALL.fullmatch(text.strip())
    if explode is not None:
        # Spark's ``explode(col)`` in a select list is a generator; sqlglot
        # renders it as ``FLATTEN(col)`` (a table function, invalid in a
        # select list). Route it through Snowpark's ``explode`` table
        # function and unwrap the VARIANT so strings are not JSON-quoted.
        return _explode_column(explode.group(2), outer=bool(explode.group(1)))
    built = _alias_predicate(text)
    return built if built is not None else F.sql_expr(_spark_sql(text))


_EXPLODE_CALL = re.compile(r"(?i)explode(_outer)?\(\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\)")


def _unaliased(column: Any) -> Any:
    """Return the column behind an ``alias`` (tracked through ``SnowColumn.alias``)."""
    return getattr(column, "_ovid2_source", column)


def _explode_column(name: str, *, outer: bool) -> Any:
    """Return a Snowpark explode of column ``name`` (marked for VARIANT unwrapping)."""
    from snowflake.snowpark import functions as F

    fn = F.explode_outer if outer else F.explode
    column = fn(F.col(_quote_name(name)))
    _EXPLODED.add(id(column))
    return column


#: ``id()`` of native explode columns built here: ``SnowFrame.select`` casts
#: their outputs (Snowflake VARIANT) back to text, as Spark yields the
#: element values themselves.
_EXPLODED: set[int] = set()


def _alias_predicate(text: str) -> Any:
    """Convert an alias-qualified Spark predicate to Snowpark columns, or ``None``."""
    import sqlglot
    from snowflake.snowpark import functions as F
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(text, read="spark")
    except Exception:
        return None
    if not any(c.table for c in tree.find_all(exp.Column)):
        return None

    def build(node: Any) -> Any:
        if isinstance(node, exp.Paren):
            return build(node.this)
        if isinstance(node, exp.And):
            return build(node.this) & build(node.expression)
        if isinstance(node, exp.Or):
            return build(node.this) | build(node.expression)
        if isinstance(node, exp.Not):
            return ~build(node.this)
        ops = {
            exp.EQ: "__eq__",
            exp.NEQ: "__ne__",
            exp.GT: "__gt__",
            exp.GTE: "__ge__",
            exp.LT: "__lt__",
            exp.LTE: "__le__",
        }
        for cls, method in ops.items():
            if isinstance(node, cls):
                return getattr(build(node.this), method)(build(node.expression))
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
            return build(node.this).is_null()
        if isinstance(node, exp.Column):
            name = str(node.name)
            if node.table:
                return F.col(_snow_alias(str(node.table)), _quote_name(name))
            return F.col(_quote_name(name))
        if isinstance(node, exp.Literal):
            return F.lit(str(node.this) if node.is_string else float(node.this))
        if isinstance(node, exp.Null):
            return F.lit(None)
        if isinstance(node, exp.Upper):
            return F.upper(build(node.this))
        if isinstance(node, exp.Lower):
            return F.lower(build(node.this))
        if isinstance(node, exp.Trim):
            return F.trim(build(node.this))
        raise _NotConvertible(type(node).__name__)

    try:
        return build(tree)
    except _NotConvertible:
        return None


class _NotConvertible(Exception):
    """Raised by :func:`_alias_predicate` for a node it does not convert."""


def _spark_sql(text: str) -> str:
    """Translate a Spark SQL expression string to Snowflake SQL (Spark semantics).

    * sqlglot transpiles the syntax;
    * a comparison of a column with a numeric literal is made lenient
      (``TRY_TO_DOUBLE(col)``) — Spark (ANSI off) casts the string side
      and yields NULL on garbage, Snowflake would raise;
    * ``CAST`` becomes ``TRY_CAST`` (NULL on bad input, as on Spark);
    * ``RLIKE`` keeps Spark's *search* semantics (``REGEXP_INSTR > 0``).
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(text, read="spark")
    except Exception:
        return text

    def _is_num(node: Any) -> bool:
        if isinstance(node, exp.Neg):
            node = node.this
        return isinstance(node, exp.Literal) and not node.is_string

    def _lenient(node: Any) -> Any:
        return exp.Anonymous(
            this="TRY_TO_DOUBLE",
            expressions=[exp.Cast(this=node, to=exp.DataType.build("VARCHAR"))],
        )

    def _fix(node: Any) -> Any:
        if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
            if isinstance(node.this, exp.Column) and _is_num(node.expression):
                node.set("this", _lenient(node.this))
            elif isinstance(node.expression, exp.Column) and _is_num(node.this):
                node.set("expression", _lenient(node.expression))
        if isinstance(node, exp.Cast):
            target = node.args["to"]
            if target.this in (
                exp.DataType.Type.VARCHAR,
                exp.DataType.Type.TEXT,
                exp.DataType.Type.CHAR,
            ):
                return exp.Cast(this=node.this, to=target)
            # Snowflake ``TRY_CAST`` takes text only; render the operand first.
            return exp.TryCast(
                this=exp.Cast(this=node.this, to=exp.DataType.build("VARCHAR")),
                to=target,
                safe=True,
            )
        if isinstance(node, exp.RegexpSplit):
            # Spark ``split(str, regex)``: Snowflake has no REGEXP_SPLIT;
            # a regex-free pattern maps to SPLIT (literal delimiter).
            pattern = node.expression
            if isinstance(pattern, exp.Literal) and pattern.is_string:
                text_pattern = str(pattern.this)
                if not any(ch in text_pattern for ch in ".^$*+?()[]{}|\\"):
                    return exp.Anonymous(this="SPLIT", expressions=[node.this, pattern])
        if isinstance(node, exp.RegexpLike):
            return exp.GT(
                this=exp.Anonymous(this="REGEXP_INSTR", expressions=[node.this, node.expression]),
                expression=exp.Literal.number(0),
            )
        return node

    # Spark identifiers are case-insensitive, back-quoted or not
    # (`id` == id); Snowflake folds *unquoted* names to upper case while a
    # quoted one is case-sensitive, and sqlglot renders Spark back-quotes as
    # double quotes. Unquote plain names first (the rule ``_quote_name``
    # applies to ``col("id")``) so every later rewrite sees them unquoted.
    for ident in tree.find_all(exp.Identifier):
        if ident.args.get("quoted") and _PLAIN_IDENTIFIER.fullmatch(str(ident.this)):
            ident.set("quoted", False)

    try:
        return str(tree.transform(_fix).sql(dialect="snowflake"))
    except Exception:
        return str(sqlglot.transpile(text, read="spark", write="snowflake")[0])


class SnowWindowSpec:
    """``WindowSpec`` with Spark's default null ordering."""

    def __init__(self, native: Any) -> None:
        self.native = native

    def partitionBy(self, *cols: Any) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindowSpec(self.native.partition_by(*[_arg(c) for c in SnowFrame._flat(cols)]))

    def orderBy(self, *cols: Any) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindowSpec(
            self.native.order_by(
                *[
                    wrap_column(c).asc().native if isinstance(c, str) else _arg(c)
                    for c in SnowFrame._flat(cols)
                ]
            )
        )

    def rowsBetween(self, start: int, end: int) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindowSpec(self.native.rows_between(_bound(start), _bound(end)))

    def rangeBetween(self, start: int, end: int) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindowSpec(self.native.range_between(_bound(start), _bound(end)))


def _bound(value: int) -> Any:
    from snowflake.snowpark import Window

    v = int(value)
    if v <= -(1 << 62):
        return Window.UNBOUNDED_PRECEDING
    if v >= (1 << 62):
        return Window.UNBOUNDED_FOLLOWING
    if v == 0:
        return Window.CURRENT_ROW
    return v


class SnowWindow:
    """``Window`` entry point."""

    unboundedPreceding = -(1 << 63)  # noqa: N815 — PySpark API
    unboundedFollowing = (1 << 63) - 1  # noqa: N815 — PySpark API
    currentRow = 0  # noqa: N815 — PySpark API

    @staticmethod
    def _spec() -> SnowWindowSpec:
        from snowflake.snowpark import Window

        return SnowWindowSpec(Window.partition_by())

    @staticmethod
    def partitionBy(*cols: Any) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindow._spec().partitionBy(*cols)

    @staticmethod
    def orderBy(*cols: Any) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindow._spec().orderBy(*cols)

    @staticmethod
    def rowsBetween(start: int, end: int) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindow._spec().rowsBetween(start, end)

    @staticmethod
    def rangeBetween(start: int, end: int) -> SnowWindowSpec:  # noqa: N802 — PySpark API
        return SnowWindow._spec().rangeBetween(start, end)


class _SnowUdf:
    """A Spark-style UDF backed by a temporary Snowflake Python UDF.

    Arguments are passed as ``VARCHAR`` (Ovid2 frames are all-string at
    stage boundaries; a typed argument is rendered to its Spark text first
    via ``CAST``), so the Python function sees the same ``str`` values a
    Spark row UDF sees on the same frame. The return value goes through
    Spark's converter for the declared type.
    """

    def __init__(
        self,
        functions: SnowFunctions,
        fn: Any,
        return_type: Any,
        *,
        vectorised: bool,
        packages: tuple[str, ...] = (),
    ) -> None:
        self._functions = functions
        self._packages = packages
        self.func = fn
        self.returnType = return_type
        self._vectorised = vectorised
        self._registered: dict[int, Any] = {}
        self.__name__ = getattr(fn, "__name__", "udf")

    def _native(self, arity: int) -> Any:
        hit = self._registered.get(arity)
        if hit is not None:
            return hit
        from snowflake.snowpark import types as T

        from ovid2.core.engines._duck_udf import _converter
        from ovid2.core.engines.types import spark_type_name

        spark_type = spark_type_name(self.returnType or "string")
        convert = _converter(spark_type)
        fn = self.func
        session = self._functions.session()
        _pickle_ovid2_by_value(fn)
        rtype = _snow_type(spark_type)
        name = f"OVID2_UDF_{next(_udf_counter)}"
        packages = ["python-dateutil", "pandas", *self._packages]
        if self._vectorised:
            from snowflake.snowpark.types import PandasSeriesType

            def _vec(*cols):  # type: ignore[no-untyped-def]  # Snowpark inspects annotations
                import pandas as pd

                # Spark hands a pandas UDF object Series with ``None`` for NULL.
                cols = tuple(
                    c.astype(object).where(c.notna(), None) if isinstance(c, pd.Series) else c
                    for c in cols
                )
                out = fn(*cols)
                return pd.Series(
                    [convert(None if v is None or v != v else v) for v in list(out)], dtype=object
                )

            udf = session.udf.register(
                _vec,
                return_type=PandasSeriesType(rtype),
                input_types=[PandasSeriesType(T.StringType())] * arity,
                name=name,
                is_permanent=False,
                replace=True,
                packages=packages,
            )
        else:

            def _row(*args):  # type: ignore[no-untyped-def]  # Snowpark inspects annotations
                return convert(fn(*args))

            udf = session.udf.register(
                _row,
                return_type=rtype,
                input_types=[T.StringType()] * arity,
                name=name,
                is_permanent=False,
                replace=True,
                packages=packages,
            )
        self._registered[arity] = udf
        return udf

    def __call__(self, *cols: Any) -> SnowColumn:
        # Arguments reach the Python function as Spark's string rendering
        # (``56.0`` for a double — not Snowflake's ``56``).
        natives = [_spark_string_cast(_arg(c)) for c in cols]
        return SnowColumn(self._native(len(natives))(*natives))

    def asNondeterministic(self) -> _SnowUdf:  # noqa: N802 — PySpark API
        return self


def _pickle_ovid2_by_value(fn: Any) -> None:
    """Ship Ovid2 / pipeline helpers *inside* the UDF pickle.

    Snowflake's Python runtime has no ``ovid2`` package, so every module a
    UDF closes over (``ovid2.legacy.udfs``, the converters in
    :mod:`._duck_udf`, the generated port's own module) is registered
    pickle-by-value — the same technique the legacy fix stage uses for
    Spark workers.
    """
    import sys

    from snowflake.snowpark import _internal

    del _internal
    import cloudpickle  # type: ignore[import-untyped]

    names = {
        "ovid2.core.engines._duck_udf",
        "ovid2.core.engines.csv_text",
        "ovid2.legacy.udfs",
        "ovid2.core.engines.types",
        getattr(fn, "__module__", "") or "",
    }
    registered: set[str] = set()
    # A UDF can reach further Ovid2 modules through its globals (e.g. a
    # generated legacy port's ``from …sparkapi.sql.functions import *``
    # pulls in ``ovid2.core.engines.dsl``). Pickle, find every ``ovid2*``
    # module the payload still imports by reference, register those too,
    # and repeat until the payload is self-contained.
    for _ in range(8):
        for name in names - registered:
            mod = sys.modules.get(name)
            if mod is not None and name.startswith("ovid2"):
                with contextlib.suppress(ValueError, ImportError, TypeError):
                    cloudpickle.register_pickle_by_value(mod)
            registered.add(name)
        try:
            payload = cloudpickle.dumps(fn)
        except Exception:  # pickling errors surface at UDF registration
            return
        referenced = _ovid2_module_refs(payload) - registered
        if not referenced:
            return
        names |= referenced


def _ovid2_module_refs(payload: bytes) -> set[str]:
    """Return the loaded ``ovid2*`` module names a pickle imports by reference."""
    import pickletools
    import sys

    refs: set[str] = set()
    for op, arg, _pos in pickletools.genops(payload):
        if (
            op.name in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE")
            and isinstance(arg, str)
            and arg.startswith("ovid2")
            and arg in sys.modules
        ):
            refs.add(arg)
    return refs


def _snow_type(spark_type: str) -> Any:
    from snowflake.snowpark import types as T

    t = spark_type.lower()
    if t.startswith("decimal"):
        inner = t[len("decimal") :].strip("() ")
        p, _, s = inner.partition(",")
        return T.DecimalType(int(p or 38), int(s or 0))
    return {
        "string": T.StringType(),
        "int": T.IntegerType(),
        "bigint": T.LongType(),
        "smallint": T.ShortType(),
        "tinyint": T.ByteType(),
        "double": T.DoubleType(),
        "float": T.FloatType(),
        "boolean": T.BooleanType(),
        "date": T.DateType(),
        "timestamp": T.TimestampType(),
    }.get(t, T.StringType())


#: Java-regex constructs Snowflake's POSIX ERE engine rejects or reads
#: differently: Unicode classes, lookarounds, word boundaries, lazy /
#: possessive quantifiers, inline flags, named groups, ``\\h`` / ``\\R``.
_NON_POSIX_REGEX = re.compile(r"\\[pPbBAzZhRQEG]|\(\?[=!<>:a-zA-Z]|[*+?}][?+]")


def _needs_python_regex(pattern: str) -> bool:
    """Return ``True`` when ``pattern`` must run in a Python UDF.

    Snowflake's regex engine is POSIX ERE (plus Perl ``\\d \\s \\w``). A
    Java pattern outside that subset raises "invalid escape sequence" or,
    worse, matches differently, so it runs in a temporary Python UDF on the
    ``regex`` module (Java-compatible ``\\p{L}``, lookarounds, ``\\b``).
    """
    return bool(_NON_POSIX_REGEX.search(pattern))


def _as_snow_column(c: Any) -> SnowColumn:
    return c if isinstance(c, SnowColumn) else SnowColumn(_arg(c))


def _python_regex_udf(kind: str, pattern: str, arg: Any = None) -> _SnowUdf:
    """Build a Python UDF for a Java regex ``search`` / ``replace`` / ``extract``."""
    functions = SnowFunctions(_active_snow_engine())
    extra = ("regex",)
    if kind == "search":

        def _search(value: str | None) -> bool | None:
            import regex  # type: ignore[import-untyped, unused-ignore]

            return None if value is None else regex.search(pattern, value) is not None

        return _SnowUdf(functions, _search, "boolean", vectorised=False, packages=extra)
    if kind == "replace":
        repl = _java_replacement_to_python(str(arg))

        def _replace(value: str | None) -> str | None:
            import regex  # type: ignore[import-untyped, unused-ignore]

            return None if value is None else regex.sub(pattern, repl, value)

        return _SnowUdf(functions, _replace, "string", vectorised=False, packages=extra)
    group = int(arg or 0)

    def _extract(value: str | None) -> str | None:
        import regex  # type: ignore[import-untyped, unused-ignore]

        if value is None:
            return None
        found = regex.search(pattern, value)
        # Spark returns "" for no match or an unmatched group.
        return "" if found is None else (found.group(group) or "")

    return _SnowUdf(functions, _extract, "string", vectorised=False, packages=extra)


def _java_replacement_to_python(replacement: str) -> str:
    """Translate a Java ``replaceAll`` replacement to ``regex.sub`` syntax.

    Java: ``$n`` is group *n*, ``\\x`` is a literal ``x``. Python: ``\\g<n>``
    is group *n*, ``\\\\`` is a literal backslash.
    """
    out: list[str] = []
    i, n = 0, len(replacement)
    while i < n:
        ch = replacement[i]
        if ch == "\\" and i + 1 < n:
            nxt = replacement[i + 1]
            out.append("\\\\" if nxt == "\\" else nxt)
            i += 2
            continue
        if ch == "$" and i + 1 < n and replacement[i + 1].isdigit():
            j = i + 1
            while j < n and replacement[j].isdigit():
                j += 1
            out.append(f"\\g<{replacement[i + 1 : j]}>")
            i = j
            continue
        out.append("\\\\" if ch == "\\" else ch)
        i += 1
    return "".join(out)


class _SessionEngine:
    """Minimal engine stand-in exposing ``native()`` for a live session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def native(self) -> Any:
        return self._session


def _active_snow_engine() -> Any:
    """Return the active Snowpark engine (DSL context, else the session)."""
    from ovid2.core.engines.dsl import active_engine

    engine = active_engine()
    if engine is not None and getattr(engine, "type_name", "") == "snowpark":
        return engine
    from snowflake.snowpark.context import get_active_session

    return _SessionEngine(get_active_session())


#: ``create_map`` literals with more string entries than this are looked up
#: through a temporary Python UDF instead of an inline ``CASE``.
_MAP_UDF_THRESHOLD = 512


def _snow_literal_value(value: Any) -> Any:
    """Return the Python value behind ``lit(x)`` (a ``create_map`` key / value).

    Raises:
        EngineOperationUnsupported: For a non-literal column (``create_map``
            supports literal keys / values only, as on DuckDB).
    """
    native = value.native if isinstance(value, SnowColumn) else value
    expression = getattr(native, "_expression", None)
    if expression is None:
        return native
    if type(expression).__name__ == "Literal":
        return expression.value
    from ovid2.core.engines.protocols import EngineOperationUnsupported

    raise EngineOperationUnsupported(
        "snowpark", "create_map", "only literal keys / values are supported"
    )


def _map_value_type(values: Any) -> str | None:
    """Spark's ``create_map`` value type for literal ``values`` (``None`` = unsupported).

    All ``str`` → ``string``; all ``int`` → ``bigint``; ``int`` / ``float`` →
    ``double``; ``bool`` → ``boolean``. ``None`` entries are ignored.
    """
    kinds = {type(v) for v in values if v is not None}
    if not kinds or kinds == {str}:
        return "string"
    if kinds == {bool}:
        return "boolean"
    if kinds == {int}:
        return "bigint"
    if kinds <= {int, float}:
        return "double"
    return None


def _spark_string(value: Any) -> str | None:
    """Render a literal as Spark's ``CAST(value AS STRING)``."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        from ovid2.core.engines._duck_udf import java_double_text

        return java_double_text(value)
    return str(value)


class SnowMap:
    """A literal ``create_map`` (supports ``m[key_col]``, Spark map lookup)."""

    def __init__(self, functions: SnowFunctions, pairs: list[tuple[Any, Any]]) -> None:
        self._functions = functions
        self._pairs = pairs
        self._lookup: _SnowUdf | None = None

    def __getitem__(self, key: Any) -> SnowColumn:
        mapping: dict[str, Any] = {}
        for k, v in self._pairs:
            mapping.setdefault(str(k), v)  # Spark keeps the first duplicate key… (LAST_WIN off)
        value_type = _map_value_type(mapping.values())
        if len(mapping) > _MAP_UDF_THRESHOLD and value_type is not None:
            # A CASE with tens of thousands of branches (zip → county)
            # aborts Snowflake's compiler (error 370001); look the key up in
            # a temporary Python UDF holding the dictionary instead. Values
            # are pre-rendered as Spark's ``CAST(v AS STRING)`` (``1``, not
            # Snowflake's ``1.0``) and returned as a string, so mixing the
            # lookup with string literals (``coalesce(m[k], '')``) behaves
            # like Spark's type widening.
            if self._lookup is None:
                frozen = {k: _spark_string(v) for k, v in mapping.items()}

                def _lookup(key: str | None) -> str | None:
                    return None if key is None else frozen.get(key)

                self._lookup = _SnowUdf(self._functions, _lookup, "string", vectorised=False)
            return self._lookup(key)
        return self._functions.map_from_dict(mapping, key, None)

    getItem = __getitem__  # noqa: N815 — PySpark API


class SnowFunctions:
    """The portable function set (:data:`~ovid2.core.engines.surface.FUNCTIONS`) on Snowpark."""

    def __init__(self, engine: Any) -> None:
        from snowflake.snowpark import functions as F

        self._F = F
        self._engine = engine

    def session(self) -> Any:
        return self._engine.native()

    # -- helpers --------------------------------------------------------------

    def _c(self, c: Any) -> Any:
        return _arg(c)

    def _w(self, native: Any) -> SnowColumn:
        return SnowColumn(native)

    def _builtin(self, name: str, *args: Any) -> SnowColumn:
        return SnowColumn(
            self._F.builtin(name)(
                *[
                    _arg(a, literal_strings=True) if not isinstance(a, SnowColumn) else a.native
                    for a in args
                ]
            )
        )

    # -- core -----------------------------------------------------------------

    def col(self, name: str) -> SnowColumn:
        return SnowColumn(_native_col(name))

    column = col

    def lit(self, value: Any) -> SnowColumn:
        if isinstance(value, SnowColumn):
            return value
        return SnowColumn(self._F.lit(value))

    def when(self, cond: Any, value: Any) -> SnowColumn:
        return SnowColumn(self._F.when(_arg(cond), _arg(value, literal_strings=True)))

    def coalesce(self, *cols: Any) -> SnowColumn:
        return SnowColumn(self._F.coalesce(*[self._c(c) for c in cols]))

    def expr(self, sql: str) -> SnowColumn:
        return SnowColumn(_sql_column(sql))

    def nvl(self, a: Any, b: Any) -> SnowColumn:
        return self.coalesce(a, b)

    ifnull = nvl

    def nullif(self, a: Any, b: Any) -> SnowColumn:
        return SnowColumn(self._F.builtin("NULLIF")(self._c(a), _arg(b, literal_strings=True)))

    def isnull(self, c: Any) -> SnowColumn:
        return SnowColumn(self._c(c).is_null())

    def isnan(self, c: Any) -> SnowColumn:
        x = self._F.builtin("TRY_TO_DOUBLE")(self._c(c).cast("string"))
        return SnowColumn(self._F.coalesce(x.equal_nan(), self._F.lit(False)))

    def nanvl(self, a: Any, b: Any) -> SnowColumn:
        x = self._c(a)
        return SnowColumn(self._F.iff(x.equal_nan(), self._c(b), x))

    def greatest(self, *cols: Any) -> SnowColumn:
        return SnowColumn(self._F.greatest(*[self._c(c) for c in cols]))

    def least(self, *cols: Any) -> SnowColumn:
        return SnowColumn(self._F.least(*[self._c(c) for c in cols]))

    def broadcast(self, df: Any) -> Any:
        return df

    def input_file_name(self) -> SnowColumn:
        # Snowflake exposes the staged file name as METADATA$FILENAME only
        # on stage scans; frames staged by Ovid2 carry no such column, so
        # Spark's "no file" value ("") is returned.
        return SnowColumn(self._F.lit(""))

    def monotonically_increasing_id(self) -> SnowColumn:
        return SnowColumn(self._F.builtin("SEQ8")())

    def rand(self, seed: int | None = None) -> SnowColumn:
        return SnowColumn(
            self._F.uniform(
                0.0, 1.0, self._F.random(seed) if seed is not None else self._F.random()
            )
        )

    # -- strings --------------------------------------------------------------

    def upper(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.upper(self._c(c)))

    def lower(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.lower(self._c(c)))

    def initcap(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.initcap(self._F.lower(self._c(c)), self._F.lit(" ")))

    def trim(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.trim(self._c(c).cast("string"), self._F.lit(" ")))

    def ltrim(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.ltrim(self._c(c).cast("string"), self._F.lit(" ")))

    def rtrim(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.rtrim(self._c(c).cast("string"), self._F.lit(" ")))

    def length(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.length(self._c(c).cast("string")))

    def substring(self, c: Any, pos: int, length: int) -> SnowColumn:
        return SnowColumn(
            self._F.substring(
                self._c(c).cast("string"), 1 if int(pos) == 0 else int(pos), int(length)
            )
        )

    def concat(self, *cols: Any) -> SnowColumn:
        return SnowColumn(self._F.concat(*[self._c(c).cast("string") for c in cols]))

    def concat_ws(self, sep: str, *cols: Any) -> SnowColumn:
        parts = [self._F.coalesce(self._c(c).cast("string"), self._F.lit(None)) for c in cols]
        return SnowColumn(
            self._F.builtin("ARRAY_TO_STRING")(
                self._F.array_construct_compact(*parts), self._F.lit(sep)
            )
        )

    def lpad(self, c: Any, length: int, pad: str) -> SnowColumn:
        return SnowColumn(self._F.lpad(self._c(c).cast("string"), int(length), self._F.lit(pad)))

    def rpad(self, c: Any, length: int, pad: str) -> SnowColumn:
        return SnowColumn(self._F.rpad(self._c(c).cast("string"), int(length), self._F.lit(pad)))

    def instr(self, c: Any, substr: str) -> SnowColumn:
        return SnowColumn(
            self._F.builtin("POSITION")(self._F.lit(substr), self._c(c).cast("string"))
        )

    def locate(self, substr: str, c: Any, pos: int = 1) -> SnowColumn:
        return SnowColumn(
            self._F.builtin("POSITION")(
                self._F.lit(substr), self._c(c).cast("string"), self._F.lit(max(1, int(pos)))
            )
        )

    def regexp_replace(self, c: Any, pattern: str, replacement: str) -> SnowColumn:
        from ovid2.core.engines._duck_expr import java_replacement_to_re2

        if _needs_python_regex(pattern):
            return _python_regex_udf("replace", pattern, replacement)(_as_snow_column(c))
        return SnowColumn(
            self._F.regexp_replace(
                self._c(c).cast("string"), pattern, java_replacement_to_re2(replacement)
            )
        )

    def regexp_extract(self, c: Any, pattern: str, idx: int) -> SnowColumn:
        if _needs_python_regex(pattern):
            return _python_regex_udf("extract", pattern, idx)(_as_snow_column(c))
        text = self._c(c).cast("string")
        # Spark yields "" (not NULL) for no match / an unmatched group.
        return SnowColumn(
            self._F.when(
                text.is_not_null(),
                self._F.coalesce(self._F.regexp_extract(text, pattern, idx), self._F.lit("")),
            )
        )

    def split(self, c: Any, pattern: str, limit: int = -1) -> SnowColumn:
        del limit
        return SnowColumn(self._F.split(self._c(c).cast("string"), self._F.lit(pattern)))

    def translate(self, c: Any, matching: str, replace: str) -> SnowColumn:
        return SnowColumn(
            self._F.translate(
                self._c(c).cast("string"), self._F.lit(matching), self._F.lit(replace)
            )
        )

    def reverse(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.reverse(self._c(c).cast("string")))

    def repeat(self, c: Any, n: int) -> SnowColumn:
        return SnowColumn(self._F.repeat(self._c(c).cast("string"), int(n)))

    def format_string(self, fmt: str, *cols: Any) -> SnowColumn:
        udf = self.udf(lambda *vals: fmt % tuple(vals), "string")
        return cast(SnowColumn, udf(*cols))

    printf = format_string

    def md5(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.md5(self._c(c).cast("string")))

    def sha1(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.sha1(self._c(c).cast("string")))

    def sha2(self, c: Any, bits: int) -> SnowColumn:
        return SnowColumn(
            self._F.sha2(self._c(c).cast("string"), 256 if int(bits) == 0 else int(bits))
        )

    def hash(self, *cols: Any) -> SnowColumn:
        return SnowColumn(self._F.hash(*[self._c(c) for c in cols]))

    xxhash64 = hash

    def base64(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.base64_encode(self._c(c).cast("string")))

    def ascii(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.ascii(self._c(c).cast("string")))

    def to_csv(self, c: Any, options: dict[str, str] | None = None) -> SnowColumn:
        from ovid2.core.engines.duckdb import _to_csv_row

        opts = dict(options or {})
        sep = str(opts.get("sep") or opts.get("delimiter") or ",")
        line_start = str(opts.get("comment", "#")) == "#"
        return cast(
            SnowColumn, self.udf(lambda row: _to_csv_row(row, sep, line_start), "string")(c)
        )

    # -- numbers --------------------------------------------------------------

    def _num(self, c: Any) -> Any:
        return self._F.builtin("TRY_TO_DOUBLE")(self._c(c).cast("string"))

    def abs(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.abs(self._num(c)))

    def round(self, c: Any, scale: int = 0) -> SnowColumn:
        return SnowColumn(self._F.round(self._num(c), int(scale)))

    def bround(self, c: Any, scale: int = 0) -> SnowColumn:
        return SnowColumn(
            self._F.builtin("ROUND")(
                self._num(c), self._F.lit(int(scale)), self._F.lit("HALF_TO_EVEN")
            )
        )

    def floor(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.floor(self._num(c)).cast("bigint"))

    def ceil(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.ceil(self._num(c)).cast("bigint"))

    ceiling = ceil

    def sqrt(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.sqrt(self._num(c)))

    def pow(self, a: Any, b: Any) -> SnowColumn:
        return SnowColumn(self._F.pow(self._num(a), self._num(b)))

    def exp(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.exp(self._num(c)))

    def log(self, *args: Any) -> SnowColumn:
        if len(args) == 2:
            return SnowColumn(self._F.log(float(args[0]), self._num(args[1])))
        return SnowColumn(self._F.ln(self._num(args[0])))

    def signum(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.sign(self._num(c)).cast("double"))

    def to_double(self, c: Any) -> SnowColumn:
        return SnowColumn(self._num(c))

    # -- dates ----------------------------------------------------------------

    def _fmt(self, fmt: str) -> str:
        from ovid2.core.engines.snowpark import _java_to_snowflake_format

        return _java_to_snowflake_format(fmt)

    def to_date(self, c: Any, fmt: str | None = None) -> SnowColumn:
        text = self._c(c).cast("string")
        if fmt is None:
            # Spark's format-less cast reads only ``yyyy[-m[m][-d[d]]]`` (an
            # optional ``T…`` / `` …`` time tail ignored); Snowflake's
            # ``TRY_TO_DATE`` would auto-detect ``MM/DD/YYYY`` and more.
            trimmed = self._F.trim(text)
            head = self._F.builtin("REGEXP_SUBSTR")(
                trimmed, self._F.lit(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")
            )
            ok = self._F.builtin("REGEXP_LIKE")(
                trimmed, self._F.lit(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}([T ].*)?")
            )
            return SnowColumn(
                self._F.when(ok, self._F.builtin("TRY_TO_DATE")(head, self._F.lit("YYYY-MM-DD")))
            )
        return SnowColumn(self._F.builtin("TRY_TO_DATE")(text, self._F.lit(self._fmt(fmt))))

    def to_timestamp(self, c: Any, fmt: str | None = None) -> SnowColumn:
        text = self._c(c).cast("string")
        if fmt is None:
            return SnowColumn(self._F.builtin("TRY_TO_TIMESTAMP_NTZ")(text))
        return SnowColumn(
            self._F.builtin("TRY_TO_TIMESTAMP_NTZ")(text, self._F.lit(self._fmt(fmt)))
        )

    def date_format(self, c: Any, fmt: str) -> SnowColumn:
        x = self._c(c)
        # Spark ``date_format`` accepts dates, timestamps and their string forms.
        ts = self._F.builtin("TRY_TO_TIMESTAMP_NTZ")(x.cast("string"))
        return SnowColumn(self._F.builtin("TO_CHAR")(ts, self._F.lit(self._fmt(fmt))))

    def datediff(self, end: Any, start: Any) -> SnowColumn:
        return SnowColumn(
            self._F.datediff("day", self.to_date(start).native, self.to_date(end).native)
        )

    def date_add(self, c: Any, days: int) -> SnowColumn:
        return SnowColumn(self._F.dateadd("day", self._F.lit(int(days)), self.to_date(c).native))

    def date_sub(self, c: Any, days: int) -> SnowColumn:
        return self.date_add(c, -int(days))

    def add_months(self, c: Any, months: int) -> SnowColumn:
        return SnowColumn(self._F.add_months(self.to_date(c).native, int(months)))

    def months_between(self, end: Any, start: Any, roundOff: bool = True) -> SnowColumn:  # noqa: N803
        out = self._F.months_between(self.to_timestamp(end).native, self.to_timestamp(start).native)
        return SnowColumn(self._F.round(out, 8) if roundOff else out)

    def current_date(self) -> SnowColumn:
        return SnowColumn(self._F.current_date())

    def current_timestamp(self) -> SnowColumn:
        return SnowColumn(self._F.builtin("LOCALTIMESTAMP")())

    def _part(self, fn: str, c: Any, *, ts: bool = False) -> SnowColumn:
        base = self.to_timestamp(c).native if ts else self.to_date(c).native
        return SnowColumn(getattr(self._F, fn)(base).cast("int"))

    def year(self, c: Any) -> SnowColumn:
        return self._part("year", c)

    def month(self, c: Any) -> SnowColumn:
        return self._part("month", c)

    def dayofmonth(self, c: Any) -> SnowColumn:
        return self._part("dayofmonth", c)

    day = dayofmonth

    def dayofweek(self, c: Any) -> SnowColumn:
        return SnowColumn((self._F.dayofweek(self.to_date(c).native) + 1).cast("int"))

    def dayofyear(self, c: Any) -> SnowColumn:
        return self._part("dayofyear", c)

    def quarter(self, c: Any) -> SnowColumn:
        return self._part("quarter", c)

    def weekofyear(self, c: Any) -> SnowColumn:
        return self._part("weekofyear", c)

    def hour(self, c: Any) -> SnowColumn:
        return self._part("hour", c, ts=True)

    def minute(self, c: Any) -> SnowColumn:
        return self._part("minute", c, ts=True)

    def second(self, c: Any) -> SnowColumn:
        return self._part("second", c, ts=True)

    def last_day(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.last_day(self.to_date(c).native))

    def trunc(self, c: Any, fmt: str) -> SnowColumn:
        unit = {"yyyy": "year", "yy": "year", "mon": "month", "mm": "month"}.get(
            fmt.lower(), fmt.lower()
        )
        return SnowColumn(self._F.date_trunc(unit, self.to_date(c).native).cast("date"))

    def date_trunc(self, fmt: str, c: Any) -> SnowColumn:
        unit = {"yyyy": "year", "yy": "year", "mon": "month", "mm": "month", "dd": "day"}.get(
            fmt.lower(), fmt.lower()
        )
        return SnowColumn(self._F.date_trunc(unit, self.to_timestamp(c).native))

    def unix_timestamp(self, c: Any = None, fmt: str = "yyyy-MM-dd HH:mm:ss") -> SnowColumn:
        if c is None:
            return SnowColumn(
                self._F.date_part("epoch_second", self._F.builtin("LOCALTIMESTAMP")())
            )
        return SnowColumn(self._F.date_part("epoch_second", self.to_timestamp(c, fmt).native))

    def from_unixtime(self, c: Any, fmt: str = "yyyy-MM-dd HH:mm:ss") -> SnowColumn:
        ts = self._F.builtin("TO_TIMESTAMP_NTZ")(
            self._F.builtin("TRY_TO_NUMBER")(self._c(c).cast("string"))
        )
        return SnowColumn(self._F.to_char(ts, self._fmt(fmt)))

    def from_utc_timestamp(self, c: Any, tz: str) -> SnowColumn:
        return SnowColumn(
            self._F.convert_timezone(
                self._F.lit(tz), self.to_timestamp(c).native, self._F.lit("UTC")
            )
        )

    def to_utc_timestamp(self, c: Any, tz: str) -> SnowColumn:
        return SnowColumn(
            self._F.convert_timezone(
                self._F.lit("UTC"), self.to_timestamp(c).native, self._F.lit(tz)
            )
        )

    # -- aggregates -----------------------------------------------------------

    def count(self, c: Any) -> SnowColumn:
        # Aggregate COUNT(col) — built by name (not an eager DataFrame action).
        return SnowColumn(self._F.builtin("COUNT")(self._c(c)))

    def countDistinct(self, *cols: Any) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self._F.count_distinct(*[self._c(c) for c in cols]))

    count_distinct = countDistinct

    def sum(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.sum(self._num(c)))

    def sumDistinct(self, c: Any) -> SnowColumn:  # noqa: N802 — PySpark API
        return SnowColumn(self._F.sum_distinct(self._num(c)))

    sum_distinct = sumDistinct

    def avg(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.avg(self._num(c)))

    mean = avg

    def min(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.min(self._c(c)))

    def max(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.max(self._c(c)))

    def first(self, c: Any, ignorenulls: bool = False) -> SnowColumn:
        del ignorenulls
        return SnowColumn(self._F.any_value(self._c(c)))

    def last(self, c: Any, ignorenulls: bool = False) -> SnowColumn:
        del ignorenulls
        return SnowColumn(self._F.any_value(self._c(c)))

    def stddev(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.stddev(self._num(c)))

    stddev_samp = stddev

    def variance(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.variance(self._num(c)))

    var_samp = variance

    def approx_count_distinct(self, c: Any, rsd: float = 0.05) -> SnowColumn:
        del rsd
        return SnowColumn(self._F.approx_count_distinct(self._c(c)))

    def collect_list(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.array_agg(self._c(c)))

    def collect_set(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.array_unique_agg(self._c(c)))

    def bool_and(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.booland_agg(self._c(c)))

    def bool_or(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.boolor_agg(self._c(c)))

    # -- windows --------------------------------------------------------------

    def row_number(self) -> SnowColumn:
        return SnowColumn(self._F.row_number())

    def rank(self) -> SnowColumn:
        return SnowColumn(self._F.rank())

    def dense_rank(self) -> SnowColumn:
        return SnowColumn(self._F.dense_rank())

    def percent_rank(self) -> SnowColumn:
        return SnowColumn(self._F.percent_rank())

    def cume_dist(self) -> SnowColumn:
        return SnowColumn(self._F.cume_dist())

    def ntile(self, n: int) -> SnowColumn:
        return SnowColumn(self._F.ntile(self._F.lit(int(n))))

    def lag(self, c: Any, offset: int = 1, default: Any = None) -> SnowColumn:
        return SnowColumn(self._F.lag(self._c(c), int(offset), default))

    def lead(self, c: Any, offset: int = 1, default: Any = None) -> SnowColumn:
        return SnowColumn(self._F.lead(self._c(c), int(offset), default))

    # -- complex types --------------------------------------------------------

    def array(self, *cols: Any) -> SnowColumn:
        if len(cols) == 1 and isinstance(cols[0], (list, tuple)):
            cols = tuple(cols[0])
        return SnowColumn(self._F.array_construct(*[self._c(c) for c in cols]))

    def struct(self, *cols: Any) -> SnowColumn:
        if len(cols) == 1 and isinstance(cols[0], (list, tuple)):
            cols = tuple(cols[0])
        pairs: list[Any] = []
        for i, c in enumerate(cols):
            name = c if isinstance(c, str) else f"col{i + 1}"
            pairs += [self._F.lit(name), self._c(c)]
        return SnowColumn(self._F.object_construct_keep_null(*pairs))

    named_struct = struct

    def create_map(self, *cols: Any) -> SnowMap:
        """Spark ``create_map(k1, v1, …)``; ``[key]`` lookup is a CASE over the literals."""
        if len(cols) == 1 and isinstance(cols[0], (list, tuple)):
            cols = tuple(cols[0])
        values = [_snow_literal_value(c) for c in cols]
        return SnowMap(self, list(zip(values[0::2], values[1::2], strict=True)))

    def explode(self, c: Any) -> SnowColumn:
        # Snowpark's ``explode`` is a table function; ``select(explode(x))``
        # is supported by Snowpark for that call shape. Its VARIANT output is
        # cast back to text by ``SnowFrame.select`` (Spark yields the values).
        column = self._F.explode(self._c(c))
        _EXPLODED.add(id(column))
        return SnowColumn(column)

    def explode_outer(self, c: Any) -> SnowColumn:
        column = self._F.explode_outer(self._c(c))
        _EXPLODED.add(id(column))
        return SnowColumn(column)

    def array_contains(self, c: Any, value: Any) -> SnowColumn:
        return SnowColumn(
            self._F.array_contains(
                self._F.to_variant(_arg(value, literal_strings=True)), self._c(c)
            )
        )

    def array_join(self, c: Any, delimiter: str, null_replacement: str | None = None) -> SnowColumn:
        del null_replacement
        return SnowColumn(self._F.builtin("ARRAY_TO_STRING")(self._c(c), self._F.lit(delimiter)))

    def size(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.coalesce(self._F.array_size(self._c(c)), self._F.lit(-1)))

    def sort_array(self, c: Any, asc: bool = True) -> SnowColumn:
        return SnowColumn(self._F.array_sort(self._c(c), asc))

    def element_at(self, c: Any, idx: Any) -> SnowColumn:
        if isinstance(idx, int):
            return SnowColumn(self._F.get(self._c(c), self._F.lit(idx - 1)))
        return SnowColumn(self._F.get(self._c(c), _arg(idx, literal_strings=True)))

    def flatten(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.builtin("ARRAY_FLATTEN")(self._c(c)))

    def array_distinct(self, c: Any) -> SnowColumn:
        return SnowColumn(self._F.array_distinct(self._c(c)))

    # -- ordering helpers -----------------------------------------------------

    def asc(self, name: str) -> SnowColumn:
        return self.col(name).asc()

    def desc(self, name: str) -> SnowColumn:
        return self.col(name).desc()

    def asc_nulls_first(self, name: str) -> SnowColumn:
        return self.col(name).asc_nulls_first()

    def asc_nulls_last(self, name: str) -> SnowColumn:
        return self.col(name).asc_nulls_last()

    def desc_nulls_first(self, name: str) -> SnowColumn:
        return self.col(name).desc_nulls_first()

    def desc_nulls_last(self, name: str) -> SnowColumn:
        return self.col(name).desc_nulls_last()

    # -- UDFs -----------------------------------------------------------------

    def udf(self, f: Any = None, returnType: Any = "string", **kwargs: Any) -> Any:  # noqa: N803
        returnType = kwargs.pop("return_type", returnType)  # noqa: N806
        if f is None or not callable(f):
            rt = f if f is not None else returnType
            return lambda fn: _SnowUdf(self, fn, rt, vectorised=False)
        return _SnowUdf(self, f, returnType, vectorised=False)

    def pandas_udf(self, f: Any = None, returnType: Any = None, functionType: Any = None) -> Any:  # noqa: N803
        del functionType
        if f is None or not callable(f):
            rt = f if f is not None else (returnType or "string")
            return lambda fn: _SnowUdf(self, fn, rt, vectorised=True)
        return _SnowUdf(self, f, returnType or "string", vectorised=True)

    # -- Ovid2 portable extensions --------------------------------------------

    def map_from_dict(
        self, mapping: dict[str, Any], key_col: Any, default: Any = None
    ) -> SnowColumn:
        sf = self._F
        if not mapping:
            return SnowColumn(sf.lit(default))
        key = self._c(key_col).cast("string")
        items = iter(mapping.items())
        k0, v0 = next(items)
        expr = sf.when(key == sf.lit(str(k0)), sf.lit(v0))
        for k, v in items:
            expr = expr.when(key == sf.lit(str(k)), sf.lit(v))
        return SnowColumn(expr.otherwise(sf.lit(default)))

    def stable_hash(self, *cols: Any) -> SnowColumn:
        sf = self._F
        parts = [sf.coalesce(self._c(c).cast("string"), sf.lit("")) for c in cols]
        return SnowColumn(sf.abs(sf.hash(*parts)).cast("string"))

    #: ``Window`` entry point (``fns.Window.partitionBy(...)``).
    Window = SnowWindow

    def __getattr__(self, name: str) -> Any:
        """Any other Spark function name → the Snowpark function of the same name."""
        if name.startswith("_"):
            raise AttributeError(name)
        native = getattr(self._F, name, None)
        if native is None:
            from ovid2.core.engines.protocols import EngineOperationUnsupported

            raise EngineOperationUnsupported("snowpark", name, "not in Snowpark's function set")
        return lambda *a, **k: _wrap_result(
            native(
                *[_arg(x, literal_strings=False) if isinstance(x, SnowColumn) else x for x in a],
                **k,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
