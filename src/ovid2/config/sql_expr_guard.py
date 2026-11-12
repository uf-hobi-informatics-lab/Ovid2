"""Allow-list guard for user-authored SQL column expressions.

User-defined transforms (``derive_column``, ``filter_expr``) accept a
Spark-SQL *column expression* string. Spark resolves such strings
against its whole function catalogue, which includes
``reflect`` / ``java_method`` (static Java calls — arbitrary code
execution in the executor), and SQL may smuggle in sub-queries that read
other tables. This module parses an expression with :mod:`sqlglot` (the
same parser the DuckDB engine transpiles with) and accepts it only when:

* it is exactly **one** expression (no ``;`` statement stacking);
* it contains no query / DDL / DML node (``SELECT``, sub-queries,
  ``INSERT``, ``DROP``, ``SET``, …);
* every function call is a known built-in from a curated, engine-portable
  allow-list (:data:`ALLOWED_FUNCTIONS`) — unknown function names
  (sqlglot ``Anonymous`` nodes, i.e. ``reflect``, UDFs, …) are rejected;
* it is at most :data:`MAX_LENGTH` characters.

It lives under :mod:`ovid2.config` (web-safe; no engine imports) so the
web tier, the MCP validator and the executor-side transform ops share one
implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

__all__ = [
    "ALLOWED_FUNCTIONS",
    "MAX_LENGTH",
    "ExprCheck",
    "SqlExprRejected",
    "check_expression",
    "ensure_safe_expression",
    "for_engine_family",
]

logger = logging.getLogger(__name__)

#: Hard ceiling on an expression's length.
MAX_LENGTH = 4000

#: Spark built-ins an expression may call, with a probe argument list used
#: to discover the sqlglot node classes each one parses to. All are
#: deterministic, row-local, and transpile to DuckDB.
_ALLOWED_PROBES: dict[str, str] = {
    # string
    "upper": "a",
    "lower": "a",
    "lcase": "a",
    "ucase": "a",
    "trim": "a",
    "ltrim": "a",
    "rtrim": "a",
    "length": "a",
    "char_length": "a",
    "substring": "a, 1, 2",
    "substr": "a, 1, 2",
    "concat": "a, b",
    "concat_ws": "'-', a, b",
    "replace": "a, 'x', 'y'",
    "regexp_replace": "a, 'x', 'y'",
    "regexp_extract": "a, 'x', 1",
    "lpad": "a, 5, '0'",
    "rpad": "a, 5, '0'",
    "initcap": "a",
    "split": "a, ','",
    "split_part": "a, ',', 1",
    "reverse": "a",
    "repeat": "a, 2",
    "left": "a, 2",
    "right": "a, 2",
    "instr": "a, 'x'",
    "locate": "'x', a",
    "translate": "a, 'x', 'y'",
    "format_string": "'%s', a",
    "ascii": "a",
    "chr": "65",
    "md5": "a",
    "sha2": "a, 256",
    "sha1": "a",
    "rlike": "a, 'x'",
    "regexp_like": "a, 'x'",
    # null / conditional
    "coalesce": "a, b",
    "nullif": "a, b",
    "nvl": "a, b",
    "nvl2": "a, b, c",
    "ifnull": "a, b",
    "if": "a > 1, b, c",
    "greatest": "a, b",
    "least": "a, b",
    "isnull": "a",
    "isnotnull": "a",
    # numeric
    "abs": "a",
    "round": "a, 2",
    "bround": "a, 2",
    "floor": "a",
    "ceil": "a",
    "ceiling": "a",
    "mod": "a, 2",
    "pow": "a, 2",
    "power": "a, 2",
    "sqrt": "a",
    "exp": "a",
    "ln": "a",
    "log": "10, a",
    "log10": "a",
    "log2": "a",
    "sign": "a",
    "signum": "a",
    "pmod": "a, 2",
    # date / time
    "to_date": "a, 'yyyy-MM-dd'",
    "to_timestamp": "a, 'yyyy-MM-dd'",
    "date_format": "a, 'yyyyMMdd'",
    "datediff": "a, b",
    "date_add": "a, 1",
    "date_sub": "a, 1",
    "add_months": "a, 1",
    "months_between": "a, b",
    "year": "a",
    "month": "a",
    "day": "a",
    "dayofmonth": "a",
    "dayofweek": "a",
    "dayofyear": "a",
    "hour": "a",
    "minute": "a",
    "second": "a",
    "quarter": "a",
    "weekofyear": "a",
    "last_day": "a",
    "trunc": "a, 'MM'",
    "date_trunc": "'MM', a",
    "current_date": "",
    "current_timestamp": "",
    "make_date": "a, b, c",
    "unix_timestamp": "a, 'yyyy'",
    "from_unixtime": "a, 'yyyy'",
    # collection (row-local)
    "array": "a, b",
    "size": "a",
    "array_contains": "a, 'x'",
    "element_at": "a, 1",
    "array_join": "a, ','",
}

#: Spark function names on the allow-list (for documentation / UI).
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(_ALLOWED_PROBES)

_ALLOWED_CLASSES: set[str] = set()
_ALLOWED_ANONYMOUS: set[str] = set()


def _allowed_classes() -> tuple[set[str], set[str]]:
    """Return ``(allowed sqlglot Func class names, allowed anonymous names)``."""
    if _ALLOWED_CLASSES or _ALLOWED_ANONYMOUS:
        return _ALLOWED_CLASSES, _ALLOWED_ANONYMOUS
    import sqlglot
    import sqlglot.expressions as exp
    from sqlglot.errors import ParseError, TokenError

    for name, args in _ALLOWED_PROBES.items():
        try:
            tree = sqlglot.parse_one(f"{name}({args})", read="spark")
        except (ParseError, TokenError):
            logger.debug("sql_expr_guard: probe %s(%s) does not parse", name, args)
            continue
        for node in tree.walk():
            if isinstance(node, exp.Anonymous):
                _ALLOWED_ANONYMOUS.add(str(node.name).lower())
            elif isinstance(node, exp.Func):
                _ALLOWED_CLASSES.add(type(node).__name__)
    return _ALLOWED_CLASSES, _ALLOWED_ANONYMOUS


#: sqlglot expression classes that are never allowed anywhere in the tree.
_FORBIDDEN_NODES: tuple[str, ...] = (
    "Select",
    "Subquery",
    "Union",
    "Intersect",
    "Except",
    "From",
    "Join",
    "Table",
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "Command",
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Copy",
    "Load",
    "Pragma",
    "Window",
    "Lambda",
    "Placeholder",
    "Parameter",
    "Exists",
)

#: Aggregates are not row-local — a column expression must not contain one.
_FORBIDDEN_AGGREGATES: frozenset[str] = frozenset(
    {"sum", "avg", "count", "min", "max", "collect_list", "collect_set", "first", "last"}
)


class SqlExprRejected(ValueError):
    """Raised when an expression fails :func:`check_expression`."""


@dataclass(frozen=True)
class ExprCheck:
    """Result of checking one expression.

    Attributes:
        ok: ``True`` when the expression is acceptable.
        errors: Human-readable rejection reasons.
        columns: Column names the expression references.
        functions: Function names the expression calls.
    """

    ok: bool
    errors: tuple[str, ...] = ()
    columns: tuple[str, ...] = field(default_factory=tuple)
    functions: tuple[str, ...] = field(default_factory=tuple)


def _func_name(node: object) -> str:
    """Return the lower-cased call name of a sqlglot function node."""
    import sqlglot.expressions as exp

    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    try:
        return str(node.sql_name()).lower()  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return type(node).__name__.lower()


def check_expression(expression: str) -> ExprCheck:
    """Check one Spark-SQL column expression against the allow-list.

    Args:
        expression: The expression text, e.g. ``"upper(trim(SEX))"``.

    Returns:
        An :class:`ExprCheck`; never raises for bad input.
    """
    if not isinstance(expression, str) or not expression.strip():
        return ExprCheck(False, ("expression is empty",))
    if len(expression) > MAX_LENGTH:
        return ExprCheck(False, (f"expression exceeds {MAX_LENGTH} characters",))
    try:
        import sqlglot
        import sqlglot.expressions as exp
        from sqlglot.errors import ParseError, TokenError
    except ImportError:  # pragma: no cover — sqlglot is a core dependency
        return ExprCheck(False, ("sqlglot is not installed; cannot validate expressions",))

    try:
        statements = [s for s in sqlglot.parse(expression, read="spark") if s is not None]
    except (ParseError, TokenError) as exc:
        return ExprCheck(False, (f"does not parse as Spark SQL: {str(exc).splitlines()[0]}",))
    if len(statements) != 1:
        return ExprCheck(False, ("exactly one expression is allowed (no ';')",))
    tree = statements[0]

    allowed_cls, allowed_anon = _allowed_classes()
    errors: list[str] = []
    columns: set[str] = set()
    functions: set[str] = set()
    for node in tree.walk():
        kind = type(node).__name__
        if kind in _FORBIDDEN_NODES:
            errors.append(f"'{kind.upper()}' is not allowed in a column expression")
            continue
        if isinstance(node, exp.Column):
            columns.add(str(node.name))
            continue
        if not isinstance(node, exp.Func):
            continue
        # Operators (AND / OR / NOT / comparisons / arithmetic) and CASE /
        # CAST are syntax, not calls.
        if isinstance(
            node, (exp.Connector, exp.Binary, exp.Unary, exp.Cast, exp.TryCast, exp.Case)
        ):
            continue
        name = _func_name(node)
        functions.add(name)
        if name in _FORBIDDEN_AGGREGATES or isinstance(node, exp.AggFunc):
            errors.append(f"aggregate '{name}' is not allowed in a row expression")
            continue
        if isinstance(node, exp.Anonymous):
            if name not in allowed_anon:
                errors.append(f"function '{name}' is not on the allow-list")
        elif kind not in allowed_cls:
            errors.append(f"function '{name}' is not on the allow-list")
    return ExprCheck(
        ok=not errors,
        errors=tuple(dict.fromkeys(errors)),
        columns=tuple(sorted(columns)),
        functions=tuple(sorted(functions)),
    )


def for_engine_family(expression: str, family: str) -> str:
    """Return ``expression`` in the dialect the engine family's ``expr`` reads.

    Spark reads Spark SQL natively and the DuckDB engine transpiles Spark
    SQL itself, so both get the text unchanged. Snowpark's
    ``functions.sql_expr`` reads Snowflake SQL, so the expression is
    transpiled Spark → Snowflake with :mod:`sqlglot`.
    """
    if family != "snowpark":
        return expression
    import sqlglot

    return str(sqlglot.transpile(expression, read="spark", write="snowflake")[0])


def ensure_safe_expression(expression: str, *, where: str = "expression") -> str:
    """Return ``expression`` unchanged, or raise :class:`SqlExprRejected`.

    Args:
        expression: Expression text.
        where: Location label for the error message.
    """
    result = check_expression(expression)
    if not result.ok:
        raise SqlExprRejected(f"{where}: " + "; ".join(result.errors))
    return expression


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.sql_expr_guard is a library module.")
