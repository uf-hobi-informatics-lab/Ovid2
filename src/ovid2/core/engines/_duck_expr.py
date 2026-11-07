"""Spark SQL expression strings -> DuckDB SQL (pure; needs ``sqlglot``).

Formatter specs and a few helpers pass Spark SQL *expression strings*
(``selectExpr``, ``functions.expr``, ``filter("...")``, join ``on:``
predicates). The DuckDB engine re-renders each through :mod:`sqlglot`
(``read="spark"`` -> ``write="duckdb"``) with Ovid-specific overrides
for the places where a mechanical transpile would change results:

* ``to_date`` / ``to_timestamp`` / ``date_format`` / ``CAST(x AS
  DATE|TIMESTAMP)`` route through :mod:`._java_datetime` (Spark LEGACY
  parser semantics, not ``strptime``).
* ``CAST(x AS INT|BIGINT|…)`` truncates toward zero like Spark (DuckDB
  rounds) and is NULL on bad input (``TRY_CAST``).
* ``x RLIKE p`` is a *search* (``regexp_matches``), as in Spark.

No DuckDB import here — the module only produces SQL text.
"""

from __future__ import annotations

import functools
from typing import Any

from ovid2.core.engines import _java_datetime as jd

__all__ = ["integral_cast_sql", "spark_cast_sql", "transpile", "transpile_condition"]

_INTEGRAL = {
    "TINYINT": "TINYINT",
    "SMALLINT": "SMALLINT",
    "INT": "INTEGER",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "LONG": "BIGINT",
    "SHORT": "SMALLINT",
    "BYTE": "TINYINT",
}


def integral_cast_sql(value_sql: str, duck_type: str) -> str:
    """Spark ``CAST(<string> AS <integral>)``: trim, truncate toward zero, NULL on error."""
    return f"TRY_CAST(trunc(TRY_CAST(trim(CAST({value_sql} AS VARCHAR)) AS DOUBLE)) AS {duck_type})"


def spark_cast_sql(value_sql: str, spark_type: str) -> str:
    """Render a Spark-semantics cast of ``value_sql`` to ``spark_type``.

    Args:
        value_sql: Rendered DuckDB SQL of the operand.
        spark_type: Spark type name (``string``, ``int``, ``double``,
            ``date``, ``timestamp``, ``decimal(10,2)``, ``boolean`` …).
    """
    t = spark_type.strip().upper()
    if t in ("STRING", "VARCHAR", "TEXT") or t.startswith("VARCHAR(") or t.startswith("CHAR("):
        return _to_string_sql(value_sql)
    if t in _INTEGRAL:
        return integral_cast_sql(value_sql, _INTEGRAL[t])
    if t in ("DOUBLE", "FLOAT", "REAL"):
        target = "DOUBLE" if t == "DOUBLE" else "FLOAT"
        return f"TRY_CAST(trim(CAST({value_sql} AS VARCHAR)) AS {target})"
    if t == "DATE":
        return f"CAST({jd.cast_sql(value_sql, timestamp=False)} AS DATE)"
    if t == "TIMESTAMP":
        return jd.cast_sql(value_sql, timestamp=True)
    if t in ("BOOLEAN", "BOOL"):
        return f"TRY_CAST(trim(CAST({value_sql} AS VARCHAR)) AS BOOLEAN)"
    if t.startswith("DECIMAL") or t.startswith("NUMERIC"):
        return f"TRY_CAST(trim(CAST({value_sql} AS VARCHAR)) AS {t})"
    return f"TRY_CAST({value_sql} AS {t})"


def _to_string_sql(value_sql: str) -> str:
    """Cast to VARCHAR rendering doubles like Spark (``1.0`` not ``1``)."""
    return f"CAST({value_sql} AS VARCHAR)"


@functools.lru_cache(maxsize=1)
def _dialect() -> Any:
    """Build (once) the sqlglot Spark dialect with Ovid's function overrides."""
    from sqlglot import exp
    from sqlglot.dialects.spark import Spark

    def _anon(name: str) -> Any:
        return lambda args: exp.Anonymous(this=name, expressions=args)

    class OvidSpark(Spark):
        class Parser(Spark.Parser):  # type: ignore[misc, valid-type]
            FUNCTIONS: dict[str, Any] = {
                **Spark.Parser.FUNCTIONS,
                "TO_DATE": _anon("__OVID_TO_DATE"),
                "TO_TIMESTAMP": _anon("__OVID_TO_TIMESTAMP"),
                "DATE_FORMAT": _anon("__OVID_DATE_FORMAT"),
            }

    return OvidSpark


def _literal_text(node: Any) -> str:
    from sqlglot import exp

    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    raise ValueError(f"datetime pattern must be a string literal, got {node.sql()!r}")


def _rewrite(node: Any) -> Any:
    """sqlglot transform: replace Spark-semantics nodes with DuckDB SQL."""
    from sqlglot import exp

    def raw(sql: str) -> Any:
        # Parenthesised so the fragment binds as one operand.
        return exp.maybe_parse(f"({sql})", dialect="duckdb")

    def render(child: Any) -> str:
        return str(child.transform(_rewrite).sql(dialect="duckdb"))

    if isinstance(node, exp.Anonymous):
        name = str(node.this).upper()
        args = list(node.expressions)
        if name == "__OVID_TO_DATE":
            value = render(args[0])
            if len(args) == 1:
                return raw(f"CAST({jd.cast_sql(value, timestamp=False)} AS DATE)")
            return raw(f"CAST({jd.parse_sql(value, _literal_text(args[1]))} AS DATE)")
        if name == "__OVID_TO_TIMESTAMP":
            value = render(args[0])
            if len(args) == 1:
                return raw(jd.cast_sql(value, timestamp=True))
            return raw(jd.parse_sql(value, _literal_text(args[1])))
        if name == "__OVID_DATE_FORMAT":
            value = render(args[0])
            fmt = jd.sql_string(jd.strftime_format(_literal_text(args[1])))
            return raw(f"strftime({_as_timestamp(value)}, {fmt})")
        return node
    if isinstance(node, (exp.Cast, exp.TryCast)):
        target = node.args["to"].sql(dialect="spark")
        return raw(spark_cast_sql(render(node.this), target))
    if isinstance(node, exp.RegexpLike):
        return raw(f"regexp_matches({render(node.this)}, {render(node.expression)})")
    if isinstance(node, exp.RegexpReplace):
        replacement = node.args.get("replacement")
        if isinstance(replacement, exp.Literal) and replacement.is_string:
            rep = jd.sql_string(java_replacement_to_re2(str(replacement.this)))
        else:
            rep = render(replacement) if replacement is not None else "''"
        return raw(f"regexp_replace({render(node.this)}, {render(node.expression)}, {rep}, 'g')")
    if isinstance(node, exp.CurrentTimestamp):
        # Spark: session-local wall clock as a plain TIMESTAMP.
        return raw("current_localtimestamp()")
    if isinstance(node, exp.Substring):
        start = node.args.get("start")
        if isinstance(start, exp.Literal) and not start.is_string and str(start.this) == "0":
            node = node.copy()
            node.set("start", exp.Literal.number(1))
        return node
    return node


def java_replacement_to_re2(replacement: str) -> str:
    """Translate a Java ``Matcher.replaceAll`` replacement to RE2 syntax.

    Java: ``$n`` is group *n*, ``\\x`` escapes ``x``. RE2 / DuckDB:
    ``\\n`` is group *n*, ``\\\\`` is a literal backslash, ``$`` is literal.
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
            out.append("\\" + replacement[i + 1 : j])
            i = j
            continue
        out.append("\\\\" if ch == "\\" else ch)
        i += 1
    return "".join(out)


def _as_timestamp(value_sql: str) -> str:
    """Coerce a date / timestamp / string operand for ``strftime``.

    Spark's ``date_format`` accepts strings by casting them with the
    format-less grammar; DuckDB's ``strftime`` needs a temporal value.
    """
    return (
        f"CASE WHEN typeof({value_sql}) = 'VARCHAR' "
        f"THEN {jd.cast_sql(value_sql, timestamp=True)} "
        f"ELSE CAST({value_sql} AS TIMESTAMP) END"
    )


def _coerce_comparisons(tree: Any, column_types: dict[str, str]) -> Any:
    """Insert Spark's implicit string->number casts in comparisons.

    Spark (ANSI off) evaluates ``string_col > 0`` numerically, NULL on
    garbage; DuckDB refuses to bind it. When one side is a VARCHAR column
    reference and the other a numeric literal, the column is wrapped in
    ``TRY_CAST(… AS DOUBLE)``.
    """
    from sqlglot import exp

    def _is_str_col(node: Any) -> bool:
        return (
            isinstance(node, exp.Column)
            and column_types.get(node.name.lower(), "").upper() == "VARCHAR"
        )

    def _is_num_lit(node: Any) -> bool:
        if isinstance(node, exp.Neg):
            node = node.this
        return isinstance(node, exp.Literal) and not node.is_string

    def _fix(node: Any) -> Any:
        if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
            left, right = node.this, node.expression
            if _is_str_col(left) and _is_num_lit(right):
                node.set("this", exp.TryCast(this=left, to=exp.DataType.build("DOUBLE")))
            elif _is_str_col(right) and _is_num_lit(left):
                node.set("expression", exp.TryCast(this=right, to=exp.DataType.build("DOUBLE")))
        return node

    return tree.transform(_fix)


def transpile_typed(spark_sql: str, column_types: dict[str, str]) -> str:
    """Like :func:`transpile`, with column-type-aware implicit casts."""
    if not column_types:
        return transpile(spark_sql)
    import sqlglot

    tree = sqlglot.parse_one(spark_sql, dialect=_dialect())
    tree = _coerce_comparisons(tree, column_types)
    return str(tree.transform(_rewrite).sql(dialect="duckdb"))


@functools.lru_cache(maxsize=4096)
def transpile(spark_sql: str) -> str:
    """Translate one Spark SQL expression (optionally ``AS alias``) to DuckDB.

    Args:
        spark_sql: A Spark SQL expression, e.g.
            ``"coalesce(date_format(to_date(d, 'yyyy-MM-dd'), 'yyyy'), '') AS y"``.

    Returns:
        The equivalent DuckDB SQL text.
    """
    import sqlglot

    tree = sqlglot.parse_one(spark_sql, dialect=_dialect())
    return str(tree.transform(_rewrite).sql(dialect="duckdb"))


def transpile_condition(spark_sql: str) -> str:
    """Alias of :func:`transpile` for boolean predicates (filter / join on)."""
    return transpile(spark_sql)
