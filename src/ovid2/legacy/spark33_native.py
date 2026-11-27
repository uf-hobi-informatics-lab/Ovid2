"""Native (DuckDB SQL) fast path for :mod:`ovid2.legacy.spark33_infer`.

Spark 3.3 CSV schema inference folds ``CSVInferSchema.inferField`` over
every value of a column. Done in Python that is one GIL-bound call per
cell; on a full-size feed (acme ``diagnosis``: 142 M rows) the fold
dominated the run while DuckDB's worker threads sat idle.

The fold is an automaton: the result only depends on which *transitions*
the column's values make. This module therefore

1. classifies every cell **in SQL** (one parallel aggregate pass) into a
   *shape class* whose transitions are known for every fold state;
2. maps each distinct value SQL could not classify (``OTHER``) to the
   class with the identical transition signature, computed with the
   exact Python :func:`~ovid2.legacy.spark33_infer.infer_field` — so
   ``10:30`` (a time-only timestamp), `` 5`` (a trimmed double) or ``NaN``
   are handled exactly, once per distinct value;
3. decides the column type from the set of classes present
   (:func:`decide`) whenever that type is independent of row order. It
   returns ``None`` otherwise and the caller runs the exact sequential fold.

=====  ===================================================  ==============
code   shape                                                standalone type
=====  ===================================================  ==============
0      NULL / ``''`` (skipped by ``inferField``)            —
1      ``[+-]?[0-9]+`` in int range, not year-like          int
2      ``[+-]?[0-9]+`` in long range, beyond int            long
3      ``[+-]?[0-9]+`` beyond long                          decimal(p, 0)
4      ``[+-]?[0-9]*\\.[0-9]+``                              double
5      valid ``YYYY-MM-DD[( |T)HH:MM:SS[.f{1,6}]]``         timestamp
6      ``true`` / ``false`` (any case)                      boolean
7      fails every parser (proven string)                   string
8      unresolved (signature did not match a class)         exact fold
9      ``[+-]?[0-9]{4,6}`` (int that also parses as a       int
       timestamp, e.g. ``2020``)
=====  ===================================================  ==============

Rendering (:func:`render_items`) is native for the shapes above and calls
the exact Python :func:`~ovid2.legacy.spark33_infer.render` (a memoised
DuckDB UDF) for every other cell. ``$OVID2_DUCKDB_NATIVE_TEXT=0`` turns
the whole fast path off.

No ``import duckdb``: everything goes through the frame's relation.
"""

from __future__ import annotations

from typing import Any

__all__ = ["infer_column_types", "native_engine", "render_items"]

#: Class codes (see module docstring).
EMPTY, INT, LONG, BIG, DEC, TS, BOOL, STR, OTHER, YEAR = range(10)

_INT_RE = r"^[+-]?[0-9]+$"
_YEAR_RE = r"^[+-]?[0-9]{4,6}$"
_DEC_RE = r"^[+-]?[0-9]*\.[0-9]+$"
_TS_RE = r"^[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?)?$"
_WS = r"[\x{0}-\x{20}]"
#: Java ``Double.parseDouble`` after ``trim`` (RE2 form of ``spark33_infer._JAVA_DOUBLE``).
#: Int / long / decimal grammars are subsets of it.
_DOUBLE_RE = (
    rf"^{_WS}*[+-]?(NaN|Infinity|([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?[fFdD]?"
    rf"|0[xX]([0-9a-fA-F]+\.?|[0-9a-fA-F]*\.[0-9a-fA-F]+)[pP][+-]?[0-9]+[fFdD]?){_WS}*$"
)
#: Necessary condition for ``parseTimestampString`` to succeed: after the
#: stripped whitespace, a leading ``T`` or a digit run (optionally signed)
#: followed by ``-`` / ``:`` or the end of the value.
_TS_CANDIDATE_RE = r"^[ \t\n\r\x{b}\x{c}]*(T|[+-]?[0-9]+([-:]|[ \t\n\r\x{b}\x{c}]*$))"
#: Longest digit string rendered natively as a double / decimal.
_MAX_NATIVE_NUMBER_LEN = 38


def native_engine(frame: Any) -> Any:
    """The DuckDB engine behind ``frame`` when the native text paths are on, else ``None``."""
    if getattr(frame, "relation", None) is None:
        return None
    from ovid2.legacy.text_io import _native_engine

    return _native_engine(frame)


def _lit(text: str) -> str:
    from ovid2.core.engines._java_datetime import sql_string

    return str(sql_string(text))


def _ts_valid_sql(c: str) -> str:
    """SQL: ``c`` (already matching :data:`_TS_RE`) is a real calendar date / time."""
    return (
        f"(try_cast(substr({c}, 1, 10) AS DATE) IS NOT NULL AND (length({c}) = 10 OR ("
        f"try_cast(substr({c}, 12, 2) AS INTEGER) <= 23 AND "
        f"try_cast(substr({c}, 15, 2) AS INTEGER) <= 59 AND "
        f"try_cast(substr({c}, 18, 2) AS INTEGER) <= 59)))"
    )


def _class_sql(c: str) -> str:
    """SQL classifying the VARCHAR expression ``c`` into a class code."""
    return (
        f"CASE WHEN {c} IS NULL OR {c} = '' THEN {EMPTY} "
        f"WHEN regexp_full_match({c}, {_lit(_INT_RE)}) THEN "
        f"  CASE WHEN regexp_full_match({c}, {_lit(_YEAR_RE)}) THEN {YEAR} "
        f"  WHEN try_cast({c} AS INTEGER) IS NOT NULL THEN {INT} "
        f"  WHEN try_cast({c} AS BIGINT) IS NOT NULL THEN {LONG} ELSE {BIG} END "
        f"WHEN regexp_full_match({c}, {_lit(_DEC_RE)}) THEN {DEC} "
        f"WHEN regexp_full_match({c}, {_lit(_TS_RE)}) AND {_ts_valid_sql(c)} THEN {TS} "
        f"WHEN lower({c}) IN ('true', 'false') THEN {BOOL} "
        f"WHEN {c} IN ('NaN', '-Inf', 'Inf') THEN {OTHER} "
        f"WHEN NOT regexp_matches({c}, {_lit(_DOUBLE_RE)}) "
        f"AND NOT regexp_matches({c}, {_lit(_TS_CANDIDATE_RE)}) THEN {STR} "
        f"ELSE {OTHER} END"
    )


def _big_precision_sql(c: str) -> str:
    """Java ``BigDecimal.precision`` of an integer literal ``c``."""
    return f"length(ltrim(ltrim(ltrim({c}, '+'), '-'), '0'))"


# ---------------------------------------------------------------------------
# Transition signatures (exact, Python)
# ---------------------------------------------------------------------------

#: Fold states a signature probes (decimal states only arise from BIG,
#: which never reaches signature matching).
_PROBE_STATES: tuple[tuple[Any, ...], ...] = (
    ("null",),
    ("int",),
    ("long",),
    ("double",),
    ("timestamp",),
    ("boolean",),
)

#: Representative value of each class.
_REPRESENTATIVES: dict[int, str] = {
    INT: "5",
    YEAR: "2020",
    LONG: "3000000000",
    DEC: "1.5",
    TS: "2020-01-02",
    BOOL: "true",
    STR: "x",
}


def _signature(value: str) -> tuple[Any, ...] | None:
    """``infer_field(state, value)`` for every probe state (``None`` if a decimal appears)."""
    from ovid2.legacy.spark33_infer import infer_field

    out = tuple(infer_field(s, value) for s in _PROBE_STATES)
    if any(t[0] == "decimal" for t in out):
        return None
    return out


_CLASS_BY_SIGNATURE: dict[tuple[Any, ...], int] | None = None


def _class_of(value: str) -> int:
    """Map an unresolved value to the class with the same transitions (``OTHER`` if none)."""
    global _CLASS_BY_SIGNATURE
    if _CLASS_BY_SIGNATURE is None:
        table: dict[tuple[Any, ...], int] = {}
        for code, rep in _REPRESENTATIVES.items():
            sig = _signature(rep)
            if sig is not None:
                table.setdefault(sig, code)
        _CLASS_BY_SIGNATURE = table
    sig = _signature(value)
    if sig is None:
        return OTHER
    return _CLASS_BY_SIGNATURE.get(sig, OTHER)


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _has(mask: int, *codes: int) -> bool:
    return any(mask & (1 << c) for c in codes)


def decide(mask: int, big_precision: int = 0) -> tuple[Any, ...] | None:
    """Return the column's Spark 3.3 inferred type from its class mask, or ``None`` (exact fold).

    Exactness per branch (``f`` = the ``inferField`` fold):

    * a STR cell turns every state into string, and string absorbs;
    * BOOL with any other typed class: string in either order (boolean is
      incompatible with every other type, no other parser accepts it);
    * TS with a non-year number: string in either order (no such number
      parses as a timestamp; ``compatibleType(number, timestamp)`` is
      undefined). TS with *year-like* integers only (``2020`` parses as a
      timestamp too) is order-dependent: exact fold;
    * DEC with integers: double in every order;
    * integers only: int / long by range; with BIG, ``decimal(p, 0)`` for
      the largest precision ``p`` (double when ``p > 38``). Long-first
      widens to ``decimal(20, 0)`` and BIG-first does not, so with LONG
      present the result is order-free only when ``p >= 20``.
    """
    if mask & (1 << STR):
        return ("string",)
    if mask & (1 << OTHER):
        return None
    typed = mask & ~(1 << EMPTY)
    if typed == 0:
        return ("string",)
    if _has(mask, BOOL):
        return ("boolean",) if typed == (1 << BOOL) else ("string",)
    if _has(mask, TS):
        if typed == (1 << TS):
            return ("timestamp",)
        if _has(mask, DEC, INT, LONG, BIG):
            return ("string",)
        return None
    if _has(mask, DEC):
        return ("double",)
    if _has(mask, BIG):
        if big_precision > 38:
            return ("double",)
        if _has(mask, LONG) and big_precision < 20:
            return None
        return ("decimal", big_precision, 0)
    if _has(mask, LONG):
        return ("long",)
    return ("int",)


def infer_column_types(frame: Any, names: list[str]) -> list[tuple[Any, ...] | None]:
    """Infer each column of ``frame`` natively; ``None`` = needs the exact sequential fold."""
    from ovid2.core.engines._duck_column import ident

    if not names:
        return []
    classes: list[str] = []
    for i, n in enumerate(names):
        c = ident(n)
        classes.append(f"{_class_sql(c)} AS {ident(f'_k{i}')}")
        classes.append(f"{c} AS {ident(f'_v{i}')}")
    aggs: list[str] = []
    for i in range(len(names)):
        k, v = ident(f"_k{i}"), ident(f"_v{i}")
        aggs.append(f"bit_or(CAST(1 AS INTEGER) << {k}) AS {ident(f'_m{i}')}")
        aggs.append(
            f"max(CASE WHEN {k} = {BIG} THEN {_big_precision_sql(v)} END) AS {ident(f'_p{i}')}"
        )
    classified = frame.relation.project(", ".join(classes))
    row = classified.aggregate(", ".join(aggs)).fetchone() or [None] * (2 * len(names))
    out: list[tuple[Any, ...] | None] = []
    for i, n in enumerate(names):
        mask = int(row[2 * i] or 0)
        precision = int(row[2 * i + 1] or 0)
        if mask & (1 << OTHER) and not mask & (1 << STR):
            mask = _resolve_other(frame, n, mask)
        out.append(decide(mask, precision))
    return out


def _resolve_other(frame: Any, name: str, mask: int) -> int:
    """Replace the ``OTHER`` bit by the classes of the column's distinct unresolved values."""
    from ovid2.core.engines._duck_column import ident

    c = ident(name)
    rel = frame.relation.filter(f"({_class_sql(c)}) = {OTHER}").project(c).distinct()
    resolved = mask & ~(1 << OTHER)
    reader = rel.fetch_record_batch(65_536)
    try:
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                break
            for value in batch.column(0).to_pylist():
                code = _class_of(value)
                if code == OTHER:
                    return mask
                if code == STR:
                    return resolved | (1 << STR)  # string absorbs every state
                resolved |= 1 << code
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    return resolved


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _python_render_sql(engine: Any, t: tuple[Any, ...], c: str) -> str:
    """SQL calling the exact Python ``render(v, t)`` on ``c`` (a memoised DuckDB UDF)."""
    from ovid2.core.engines._duck_udf import DuckUdf
    from ovid2.legacy.spark33_infer import render

    fn = DuckUdf(lambda v, tt=t: render(v, tt), "string")
    return f"{fn.sql_name(engine._root_connection())}({c})"


def render_sql(engine: Any, t: tuple[Any, ...], c: str) -> str:
    """SQL rendering VARCHAR ``c`` as the CSV-writer text of type ``t`` (``render`` parity)."""
    kind = t[0]
    if kind == "string":
        return c
    empty = f"{c} IS NULL OR {c} = ''"
    if kind == "boolean":
        return f"CASE WHEN {empty} THEN NULL WHEN lower({c}) = 'true' THEN 'true' ELSE 'false' END"
    slow = _python_render_sql(engine, t, c)
    is_int = f"regexp_full_match({c}, {_lit(_INT_RE)})"
    if kind in ("int", "long"):
        return (
            f"CASE WHEN {empty} THEN NULL WHEN {is_int} AND try_cast({c} AS BIGINT) IS NOT NULL "
            f"THEN CAST(CAST({c} AS BIGINT) AS VARCHAR) ELSE {slow} END"
        )
    if kind == "decimal" and t[2] == 0:
        return (
            f"CASE WHEN {empty} THEN NULL WHEN {is_int} AND length({c}) <= {_MAX_NATIVE_NUMBER_LEN} "
            f"THEN CAST(CAST({c} AS HUGEINT) AS VARCHAR) ELSE {slow} END"
        )
    if kind == "double":
        return (
            f"CASE WHEN {empty} THEN NULL WHEN length({c}) <= {_MAX_NATIVE_NUMBER_LEN} AND "
            f"({is_int} OR regexp_full_match({c}, {_lit(_DEC_RE)})) "
            f"THEN __ovid2_java_double(CAST({c} AS DOUBLE)) ELSE {slow} END"
        )
    if kind == "timestamp":
        return (
            f"CASE WHEN {empty} THEN NULL "
            f"WHEN regexp_full_match({c}, {_lit(_TS_RE)}) AND {_ts_valid_sql(c)} THEN "
            f"substr({c}, 1, 10) || 'T' || CASE WHEN length({c}) = 10 THEN '00:00:00' "
            f"ELSE substr({c}, 12, 8) END || '.' || "
            f"rpad(substr({c}, 21, 3), 3, '0') || 'Z' ELSE {slow} END"
        )
    return slow


def render_items(engine: Any, types: list[tuple[Any, ...]], names: list[str]) -> list[str]:
    """``select_native`` items rendering column ``names[i]`` as type ``types[i]``."""
    from ovid2.core.engines._duck_column import ident

    return [
        f"{render_sql(engine, t, ident(n))} AS {ident(n)}"
        for t, n in zip(types, names, strict=True)
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
