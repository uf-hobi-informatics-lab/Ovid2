"""PySpark-shaped ``Column`` for the DuckDB engine (pure; no duckdb import).

A :class:`DuckColumn` is a *deferred* DuckDB SQL expression. It is
rendered against a :class:`RenderCtx` supplied by the DataFrame that
finally consumes it, because two things are only known at that point:

* **Qualifier resolution.** ``df["x"]`` / ``col("alias.x")`` carry a
  Spark-style qualifier. Spark resolves it by attribute identity, so the
  reference survives later projections; DuckDB only knows the aliases
  currently in scope. :meth:`RenderCtx.qualify` maps a qualifier to the
  visible alias that descends from it, or drops it (unqualified name)
  when the column passed through a projection.
* **Operand types.** Spark coerces implicitly (``string_col > 0`` casts
  the string to a number, ANSI off -> NULL on garbage); DuckDB refuses to
  bind such comparisons. Each column knows its *kind* (``str`` / ``num``
  / ``bool`` / ``date`` / ``ts`` / ``null`` / ``any``) so operators can
  insert the cast Spark would have inserted.

ovid2: allow-sql-fstring — renders SQL *expression* fragments only.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ovid2.core.engines._duck_expr import spark_cast_sql

__all__ = ["DuckColumn", "RenderCtx", "ident", "literal_sql", "to_column"]

_KIND_BY_DUCK_TYPE: tuple[tuple[str, str], ...] = (
    ("VARCHAR", "str"),
    ("TIMESTAMP", "ts"),
    ("DATE", "date"),
    ("BOOLEAN", "bool"),
    ("INTEGER", "num"),
    ("BIGINT", "num"),
    ("HUGEINT", "num"),
    ("SMALLINT", "num"),
    ("TINYINT", "num"),
    ("UBIGINT", "num"),
    ("UINTEGER", "num"),
    ("DOUBLE", "num"),
    ("FLOAT", "num"),
    ("DECIMAL", "num"),
)


def kind_of_duck_type(duck_type: str) -> str:
    """Map a DuckDB type name to a column kind."""
    upper = duck_type.upper()
    for prefix, kind in _KIND_BY_DUCK_TYPE:
        if upper.startswith(prefix):
            return kind
    return "any"


def ident(name: str) -> str:
    """Quote ``name`` as a DuckDB identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def literal_sql(value: Any) -> tuple[str, str]:
    """Return ``(sql, kind)`` for a Python literal (Spark ``lit`` rules)."""
    if value is None:
        return "NULL", "null"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), "bool"
    if isinstance(value, int):
        return str(value), "num"
    if isinstance(value, float):
        if value != value:  # NaN
            return "CAST('NaN' AS DOUBLE)", "num"
        if value in (float("inf"), float("-inf")):
            return f"CAST('{'-' if value < 0 else ''}inf' AS DOUBLE)", "num"
        return f"CAST({value!r} AS DOUBLE)", "num"
    if isinstance(value, Decimal):
        return f"CAST('{value}' AS DECIMAL(38, {max(0, -value.as_tuple().exponent)}))", "num"  # type: ignore[operator]
    if isinstance(value, _dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'", "ts"
    if isinstance(value, _dt.date):
        return f"DATE '{value.isoformat()}'", "date"
    text = str(value)
    return "'" + text.replace("'", "''") + "'", "str"


@dataclass
class ScopeEntry:
    """One relation visible to an expression.

    Attributes:
        alias: The DuckDB alias the relation is visible under.
        lineage: Every Spark-level qualifier that resolves to it
            (ancestor aliases plus user ``.alias(...)`` names).
        types: ``lower(column) -> duck type`` of the relation.
    """

    alias: str
    lineage: frozenset[str]
    types: dict[str, str] = field(default_factory=dict)


@dataclass
class RenderCtx:
    """The scope an expression is rendered in (built by the DataFrame)."""

    scope: tuple[ScopeEntry, ...] = ()

    def entry_for(self, qualifier: str, name: str | None = None) -> ScopeEntry | None:
        """Return the scope entry ``qualifier`` resolves to, or ``None``.

        An exact alias match wins; otherwise the first entry whose lineage
        contains ``qualifier`` *and* (when ``name`` is given) exposes the
        column, falling back to the first lineage match.
        """
        for entry in self.scope:
            if qualifier == entry.alias:
                return entry
        lineage_hits = [e for e in self.scope if qualifier in e.lineage]
        if name is not None:
            for entry in lineage_hits:
                if name.lower() in entry.types:
                    return entry
        return lineage_hits[0] if lineage_hits else None

    def qualify(self, qualifier: str | None, name: str) -> str:
        """Return the SQL reference for ``qualifier.name`` in this scope."""
        if qualifier is None:
            return ident(name)
        entry = self.entry_for(qualifier, name)
        if entry is not None:
            return f"{ident(entry.alias)}.{ident(name)}"
        return ident(name)

    def type_of(self, qualifier: str | None, name: str) -> str | None:
        """Return the DuckDB type of a referenced column, if known."""
        key = name.lower()
        if qualifier is not None:
            entry = self.entry_for(qualifier, name)
            if entry is not None:
                return entry.types.get(key)
        for entry in self.scope:
            if key in entry.types:
                return entry.types[key]
        return None


Renderer = Callable[[RenderCtx], str]
KindFn = Callable[[RenderCtx], str]


def _const(kind: str) -> KindFn:
    return lambda _ctx: kind


class DuckColumn:
    """A deferred, PySpark-``Column``-shaped DuckDB expression.

    Only the subset of the Spark ``Column`` API used by Ovid2 is
    provided; everything is pure string building (no query runs).
    """

    __slots__ = ("_case", "_kind", "_lit", "_name", "_null_literal", "_order", "_render")

    def __init__(
        self,
        render: Renderer,
        kind: KindFn | str = "any",
        *,
        name: str | None = None,
        null_literal: bool = False,
        order: str | None = None,
        case: tuple[list[tuple[DuckColumn, DuckColumn]], DuckColumn | None] | None = None,
    ) -> None:
        self._render = render
        self._kind = _const(kind) if isinstance(kind, str) else kind
        self._name = name
        self._null_literal = null_literal
        self._order = order
        self._case = case
        #: ``(value,)`` for a ``lit(value)`` column, else ``None``.
        self._lit: tuple[Any] | None = None

    # ---- construction helpers ------------------------------------------

    @staticmethod
    def ref(qualifier: str | None, name: str) -> DuckColumn:
        """Return a column reference (``col('x')`` / ``df['x']``)."""

        def _render(ctx: RenderCtx) -> str:
            return ctx.qualify(qualifier, name)

        def _kind(ctx: RenderCtx) -> str:
            duck_type = ctx.type_of(qualifier, name)
            return kind_of_duck_type(duck_type) if duck_type else "any"

        return DuckColumn(_render, _kind, name=name)

    @staticmethod
    def literal(value: Any) -> DuckColumn:
        """Return a literal column (Spark ``lit``)."""
        sql, kind = literal_sql(value)
        column = DuckColumn(
            lambda _ctx: sql,
            kind,
            name=None if value is None else str(value),
            null_literal=value is None,
        )
        column._lit = (value,)
        return column

    @property
    def literal_value(self) -> tuple[Any] | None:
        """``(value,)`` when this column is a plain ``lit(value)``, else ``None``."""
        return self._lit

    @staticmethod
    def raw(sql: str, kind: str = "any", name: str | None = None) -> DuckColumn:
        """Wrap a context-free SQL fragment."""
        return DuckColumn(lambda _ctx: sql, kind, name=name)

    @staticmethod
    def func(name: str, *args: Any, kind: str = "any") -> DuckColumn:
        """Return ``name(arg, ...)`` over columns / literals."""
        cols = [to_column(a, literal_strings=True) for a in args]

        def _render(ctx: RenderCtx) -> str:
            return f"{name}({', '.join(c.sql(ctx) for c in cols)})"

        return DuckColumn(_render, kind)

    @staticmethod
    def template(fmt: Callable[..., str], *args: Any, kind: str | KindFn = "any") -> DuckColumn:
        """Return a column whose SQL is ``fmt(*rendered_args)``."""
        cols = [to_column(a, literal_strings=True) for a in args]

        def _render(ctx: RenderCtx) -> str:
            return fmt(*[c.sql(ctx) for c in cols])

        return DuckColumn(_render, kind)

    # ---- rendering -----------------------------------------------------

    def sql(self, ctx: RenderCtx) -> str:
        """Render the bare expression (no alias)."""
        if self._case is not None:
            return self._render_case(ctx)
        return self._render(ctx)

    def kind(self, ctx: RenderCtx) -> str:
        """Return the expression's kind in ``ctx``."""
        if self._case is not None:
            kinds = {v.kind(ctx) for _c, v in self._case[0]}
            if self._case[1] is not None:
                kinds.add(self._case[1].kind(ctx))
            kinds.discard("null")
            if "str" in kinds:
                return "str"
            return kinds.pop() if len(kinds) == 1 else "any"
        return self._kind(ctx)

    def projection_sql(self, ctx: RenderCtx) -> str:
        """Render ``expr AS "name"`` for a ``select`` list."""
        expr = self.sql(ctx)
        if self._null_literal:
            # Spark's NullType: keep it a VARCHAR so later coalesce /
            # union / CSV writes never trip over DuckDB's INTEGER NULL.
            expr = "CAST(NULL AS VARCHAR)"
        if self._name is not None:
            return f"{expr} AS {ident(self._name)}"
        return expr

    @property
    def output_name(self) -> str | None:
        """Return the alias / referenced column name, if any."""
        return self._name

    def order_sql(self, ctx: RenderCtx) -> str:
        """Render for ``ORDER BY`` (Spark: asc nulls first, desc nulls last)."""
        return f"{self.sql(ctx)} {self._order or 'ASC NULLS FIRST'}"

    def _render_case(self, ctx: RenderCtx) -> str:
        assert self._case is not None
        branches, default = self._case
        values = [v for _c, v in branches] + ([default] if default is not None else [])
        kinds = {v.kind(ctx) for v in values}
        stringify = "str" in kinds and bool(kinds & {"num", "bool", "date", "ts"})
        parts = ["CASE"]
        for cond, value in branches:
            parts.append(f"WHEN {cond.sql(ctx)} THEN {_coerced(value, ctx, stringify)}")
        if default is not None:
            parts.append(f"ELSE {_coerced(default, ctx, stringify)}")
        parts.append("END")
        return " ".join(parts)

    # ---- Column API ----------------------------------------------------

    def alias(self, *names: str, **_kwargs: Any) -> DuckColumn:
        """Rename the output column."""
        clone = self._clone()
        clone._name = names[0]
        return clone

    name = alias

    def cast(self, data_type: Any) -> DuckColumn:
        """Spark-semantics cast (``TRY_CAST``; integral casts truncate)."""
        spark_type = data_type if isinstance(data_type, str) else _type_name(data_type)
        target = spark_type.strip().lower()
        kind = {
            "string": "str",
            "varchar": "str",
            "date": "date",
            "timestamp": "ts",
            "boolean": "bool",
        }.get(target, "num")
        inner = self

        def _render(ctx: RenderCtx) -> str:
            return spark_cast_sql(inner.sql(ctx), spark_type)

        return DuckColumn(_render, kind, name=self._name)

    astype = cast

    def isNull(self) -> DuckColumn:  # noqa: N802 — PySpark API
        """``IS NULL``."""
        return self._postfix("IS NULL")

    def isNotNull(self) -> DuckColumn:  # noqa: N802 — PySpark API
        """``IS NOT NULL``."""
        return self._postfix("IS NOT NULL")

    def isin(self, *values: Any) -> DuckColumn:
        """``IN (...)``; accepts varargs or a single list."""
        if len(values) == 1 and isinstance(values[0], (list, tuple, set, frozenset)):
            values = tuple(values[0])
        if not values:
            return DuckColumn.raw("FALSE", "bool")
        cols = [to_column(v, literal_strings=True) for v in values]
        inner = self

        def _render(ctx: RenderCtx) -> str:
            left_kind = inner.kind(ctx)
            items = ", ".join(c.sql(ctx) for c in cols)
            left = inner.sql(ctx)
            if left_kind == "str" and any(c.kind(ctx) == "num" for c in cols):
                left = f"TRY_CAST({left} AS DOUBLE)"
            return f"({left} IN ({items}))"

        return DuckColumn(_render, "bool")

    def isNotIn(self, *values: Any) -> DuckColumn:  # noqa: N802
        """Negated :meth:`isin`."""
        return ~self.isin(*values)

    def rlike(self, pattern: str) -> DuckColumn:
        """Regex *search* (Spark ``RLIKE``)."""
        return DuckColumn.template(
            lambda a, p: f"regexp_matches({a}, {p})", self, pattern, kind="bool"
        )

    def like(self, pattern: str) -> DuckColumn:
        """SQL ``LIKE``."""
        return self._binary("LIKE", pattern, kind="bool", numeric_coerce=False)

    def ilike(self, pattern: str) -> DuckColumn:
        """SQL ``ILIKE``."""
        return self._binary("ILIKE", pattern, kind="bool", numeric_coerce=False)

    def startswith(self, prefix: Any) -> DuckColumn:
        """Prefix test."""
        return DuckColumn.template(lambda a, p: f"starts_with({a}, {p})", self, prefix, kind="bool")

    def endswith(self, suffix: Any) -> DuckColumn:
        """Suffix test."""
        return DuckColumn.template(lambda a, p: f"suffix({a}, {p})", self, suffix, kind="bool")

    def contains(self, other: Any) -> DuckColumn:
        """Substring test."""
        return DuckColumn.template(lambda a, p: f"contains({a}, {p})", self, other, kind="bool")

    def substr(self, start: int, length: int) -> DuckColumn:
        """Spark ``substr`` (position 0 behaves like 1)."""
        pos = 1 if start == 0 else start
        return DuckColumn.template(lambda a: f"substring({a}, {pos}, {length})", self, kind="str")

    def between(self, low: Any, high: Any) -> DuckColumn:
        """Inclusive range test."""
        lower: DuckColumn = self >= low
        upper: DuckColumn = self <= high
        return lower & upper

    def eqNullSafe(self, other: Any) -> DuckColumn:  # noqa: N802
        """``<=>``."""
        return self._binary("IS NOT DISTINCT FROM", other, kind="bool")

    def when(self, condition: DuckColumn, value: Any) -> DuckColumn:
        """Chain another ``WHEN`` onto a ``when(...)`` column."""
        if self._case is None:
            raise TypeError("when() can only be chained on a when(...) column")
        branches, default = self._case
        if default is not None:
            raise TypeError("when() cannot follow otherwise()")
        new = list(branches) + [(condition, to_column(value, literal_strings=True))]
        return DuckColumn(lambda _c: "", case=(new, None), name=self._name)

    def otherwise(self, value: Any) -> DuckColumn:
        """Close a ``when(...)`` chain."""
        if self._case is None:
            raise TypeError("otherwise() can only be applied on a when(...) column")
        branches, default = self._case
        if default is not None:
            raise TypeError("otherwise() can only be applied once")
        return DuckColumn(
            lambda _c: "",
            case=(list(branches), to_column(value, literal_strings=True)),
            name=self._name,
        )

    def asc(self) -> DuckColumn:
        """Ascending sort key (nulls first)."""
        return self._with_order("ASC NULLS FIRST")

    def desc(self) -> DuckColumn:
        """Descending sort key (nulls last)."""
        return self._with_order("DESC NULLS LAST")

    def asc_nulls_last(self) -> DuckColumn:
        """Ascending, nulls last."""
        return self._with_order("ASC NULLS LAST")

    def desc_nulls_first(self) -> DuckColumn:
        """Descending, nulls first."""
        return self._with_order("DESC NULLS FIRST")

    def bitwiseAND(self, other: Any) -> DuckColumn:  # noqa: N802
        """Bitwise AND."""
        return self._binary("&", other, kind="num", numeric_coerce=False)

    def getItem(self, key: Any) -> DuckColumn:  # noqa: N802
        """Array (0-based, Spark) / struct field access."""
        if isinstance(key, int):
            return DuckColumn.template(lambda a: f"({a})[{key + 1}]", self)
        return DuckColumn.template(lambda a, k: f"({a})[{k}]", self, key)

    def over(self, window: Any) -> DuckColumn:
        """Window application (``window`` renders its own ``OVER`` clause)."""
        inner = self
        return DuckColumn(lambda ctx: f"{inner.sql(ctx)} OVER ({window.sql(ctx)})", self._kind)

    # ---- operators -----------------------------------------------------

    def __eq__(self, other: Any) -> DuckColumn:  # type: ignore[override]
        return self._binary("=", other, kind="bool")

    def __ne__(self, other: Any) -> DuckColumn:  # type: ignore[override]
        return self._binary("<>", other, kind="bool")

    def __lt__(self, other: Any) -> DuckColumn:
        return self._binary("<", other, kind="bool")

    def __le__(self, other: Any) -> DuckColumn:
        return self._binary("<=", other, kind="bool")

    def __gt__(self, other: Any) -> DuckColumn:
        return self._binary(">", other, kind="bool")

    def __ge__(self, other: Any) -> DuckColumn:
        return self._binary(">=", other, kind="bool")

    def __and__(self, other: Any) -> DuckColumn:
        return self._binary("AND", other, kind="bool", numeric_coerce=False)

    def __or__(self, other: Any) -> DuckColumn:
        return self._binary("OR", other, kind="bool", numeric_coerce=False)

    def __rand__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True) & self

    def __ror__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True) | self

    def __invert__(self) -> DuckColumn:
        inner = self
        return DuckColumn(lambda ctx: f"(NOT {inner.sql(ctx)})", "bool")

    def __neg__(self) -> DuckColumn:
        inner = self
        return DuckColumn(lambda ctx: f"(- {_numeric(inner, ctx)})", "num")

    def __add__(self, other: Any) -> DuckColumn:
        return self._arith("+", other)

    def __radd__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True)._arith("+", self)

    def __sub__(self, other: Any) -> DuckColumn:
        return self._arith("-", other)

    def __rsub__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True)._arith("-", self)

    def __mul__(self, other: Any) -> DuckColumn:
        return self._arith("*", other)

    def __rmul__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True)._arith("*", self)

    def __truediv__(self, other: Any) -> DuckColumn:
        return self._arith("/", other)

    def __rtruediv__(self, other: Any) -> DuckColumn:
        return to_column(other, literal_strings=True)._arith("/", self)

    def __mod__(self, other: Any) -> DuckColumn:
        return self._arith("%", other)

    def __bool__(self) -> bool:
        raise ValueError(
            "Cannot convert a DuckColumn into bool: use '&' for 'and', '|' for 'or', "
            "'~' for 'not' when building DataFrame boolean expressions."
        )

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        try:
            return f"DuckColumn<{self.sql(RenderCtx())}>"
        except Exception:
            return "DuckColumn<?>"

    # ---- internals -----------------------------------------------------

    def _clone(self) -> DuckColumn:
        clone = DuckColumn(
            self._render,
            self._kind,
            name=self._name,
            null_literal=self._null_literal,
            order=self._order,
            case=self._case,
        )
        clone._lit = self._lit
        return clone

    def _with_order(self, order: str) -> DuckColumn:
        clone = self._clone()
        clone._order = order
        return clone

    def _postfix(self, op: str) -> DuckColumn:
        inner = self
        return DuckColumn(lambda ctx: f"({inner.sql(ctx)} {op})", "bool")

    def _binary(self, op: str, other: Any, *, kind: str, numeric_coerce: bool = True) -> DuckColumn:
        left = self
        right = to_column(other, literal_strings=True)

        def _render(ctx: RenderCtx) -> str:
            lsql, rsql = left.sql(ctx), right.sql(ctx)
            if numeric_coerce:
                lk, rk = left.kind(ctx), right.kind(ctx)
                # Spark (ANSI off) compares string vs number numerically,
                # yielding NULL on unparseable strings.
                if lk == "str" and rk == "num":
                    lsql = f"TRY_CAST({lsql} AS DOUBLE)"
                elif rk == "str" and lk == "num":
                    rsql = f"TRY_CAST({rsql} AS DOUBLE)"
                elif {lk, rk} == {"str", "date"} or {lk, rk} == {"str", "ts"}:
                    target = "DATE" if "date" in (lk, rk) else "TIMESTAMP"
                    if lk == "str":
                        lsql = f"TRY_CAST({lsql} AS {target})"
                    else:
                        rsql = f"TRY_CAST({rsql} AS {target})"
                elif {lk, rk} == {"str", "bool"}:
                    if lk == "str":
                        lsql = f"TRY_CAST({lsql} AS BOOLEAN)"
                    else:
                        rsql = f"TRY_CAST({rsql} AS BOOLEAN)"
            return f"({lsql} {op} {rsql})"

        return DuckColumn(_render, kind)

    def _arith(self, op: str, other: Any) -> DuckColumn:
        left = self
        right = to_column(other, literal_strings=True)

        def _render(ctx: RenderCtx) -> str:
            return f"({_numeric(left, ctx)} {op} {_numeric(right, ctx)})"

        return DuckColumn(_render, "num")


def _numeric(col: DuckColumn, ctx: RenderCtx) -> str:
    """Render ``col`` for arithmetic, casting strings like Spark does."""
    sql = col.sql(ctx)
    if col.kind(ctx) in ("str", "null"):
        return f"TRY_CAST({sql} AS DOUBLE)"
    return sql


def _coerced(col: DuckColumn, ctx: RenderCtx, stringify: bool) -> str:
    sql = col.sql(ctx)
    if col._null_literal:
        return "NULL"
    if stringify and col.kind(ctx) != "str":
        return f"CAST({sql} AS VARCHAR)"
    return sql


def _type_name(data_type: Any) -> str:
    """Best-effort Spark type name for a ``pyspark.sql.types``-like object."""
    simple = getattr(data_type, "simpleString", None)
    if callable(simple):
        return str(simple())
    return str(data_type)


def to_column(value: Any, *, literal_strings: bool = False) -> DuckColumn:
    """Coerce ``value`` to a :class:`DuckColumn`.

    Strings become column references unless ``literal_strings`` is set
    (operator right-hand sides: ``col == "x"`` compares to a literal).
    """
    if isinstance(value, DuckColumn):
        return value
    if isinstance(value, str) and not literal_strings:
        return column_from_name(value)
    return DuckColumn.literal(value)


def column_from_name(name: str) -> DuckColumn:
    """Parse ``"x"`` / ``"alias.x"`` / ``"`a.b`"`` into a reference."""
    if name == "*":
        return DuckColumn.raw("*", name=None)
    if name.startswith("`") and name.endswith("`"):
        return DuckColumn.ref(None, name[1:-1].replace("``", "`"))
    if "." in name:
        qualifier, _, column = name.partition(".")
        if column == "*":
            return DuckColumn(lambda ctx: f"{ctx.qualify(qualifier, '__x').rsplit('.', 1)[0]}.*")
        return DuckColumn.ref(qualifier, column)
    return DuckColumn.ref(None, name)


def columns_from_args(args: Iterable[Any]) -> list[DuckColumn]:
    """Flatten ``select("a", "b")`` / ``select(["a", "b"])`` arguments."""
    out: list[DuckColumn] = []
    for arg in args:
        if isinstance(arg, (list, tuple)):
            out.extend(columns_from_args(arg))
        else:
            out.append(to_column(arg))
    return out
