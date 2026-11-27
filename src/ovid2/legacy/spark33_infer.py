"""Spark 3.3 CSV schema inference, ported for non-Spark engines.

Legacy ``cf.append_additional_fields`` re-read ``deduplicated_<t>.csv``
with ``inferSchema="true"`` on Spark 3.3 (UTC JVM, ``timeParserPolicy``
EXCEPTION, no ``preferDate``) and wrote the inferred *values* back out;
see :func:`ovid2.legacy.csv_roundtrip.legacy_infer_schema_view`. Spark
engines reproduce that by running Spark. This module reproduces it for
every other engine:

* :func:`infer_type` — ``CSVInferSchema.inferField`` + ``compatibleType``
  (int → long → decimal(p, 0) → double → timestamp → boolean → string,
  with the CSV-specific promotions).
* :func:`render` — the CSV writer's text for a value of the inferred
  type (``Long`` / ``BigDecimal.toString`` / Java ``Double.toString`` /
  ``yyyy-MM-dd'T'HH:mm:ss.SSSXXX`` in UTC / ``true`` / ``false``).
* :func:`infer_and_render` — applies both to a frame (column-wise type
  inference over all rows, then rendering), exactly as Spark's reader
  then writer did.

Pure Python over values (no engine imports); the frame plumbing uses
the engine-dispatching DSL.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["infer_and_render", "infer_type", "infer_typed", "parse_timestamp", "render"]

_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1
_LONG_MIN, _LONG_MAX = -(2**63), 2**63 - 1

#: Java ``Integer.parseInt`` / ``Long.parseLong`` grammar.
_JAVA_INT = re.compile(r"[+-]?[0-9]+")
#: Java ``new BigDecimal(String)`` grammar.
_JAVA_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
#: Java ``Double.parseDouble`` grammar (after Java ``trim``).
_JAVA_DOUBLE = re.compile(
    r"[+-]?(?:NaN|Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[fFdD]?"
    r"|0[xX](?:[0-9a-fA-F]+\.?|[0-9a-fA-F]*\.[0-9a-fA-F]+)[pP][+-]?[0-9]+[fFdD]?)"
)


def _java_trim(text: str) -> str:
    i, j = 0, len(text)
    while i < j and text[i] <= " ":
        i += 1
    while j > i and text[j - 1] <= " ":
        j -= 1
    return text[i:j]


def _parse_int(text: str, lo: int, hi: int) -> bool:
    if not _JAVA_INT.fullmatch(text):
        return False
    return lo <= int(text) <= hi


def _parse_decimal(text: str) -> Decimal | None:
    if not _JAVA_DECIMAL.fullmatch(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _decimal_precision_scale(value: Decimal) -> tuple[int, int]:
    """Java ``BigDecimal.precision`` / ``scale``."""
    sign, digits, exponent = value.as_tuple()
    del sign
    scale = -int(exponent)
    unscaled = digits
    stripped = list(unscaled)
    while len(stripped) > 1 and stripped[0] == 0:
        stripped.pop(0)
    precision = len(stripped)
    return precision, scale


def _parse_double(text: str) -> bool:
    return bool(_JAVA_DOUBLE.fullmatch(_java_trim(text)))


def _parse_boolean(text: str) -> bool:
    return text.lower() in ("true", "false")


# ---------------------------------------------------------------------------
# Timestamps (DateTimeUtils.parseTimestampString / stringToTimestamp)
# ---------------------------------------------------------------------------


def _valid_digits(segment: int, digits: int) -> bool:
    return (
        segment == 6
        or (segment == 0 and 4 <= digits <= 6)
        or (segment == 7 and digits <= 2)
        or (segment not in (0, 6, 7) and 0 < digits <= 2)
    )


def _parse_timestamp_string(text: str) -> tuple[list[int], str | None, bool] | None:
    """Port of ``DateTimeUtils.parseTimestampString`` (Spark 3.3)."""
    s = text.strip(" \t\n\r\x0b\x0c")
    if not s:
        return None
    data = s.encode("utf-8")
    seg = [1, 1, 1, 0, 0, 0, 0, 0, 0]
    i = 0
    value = 0
    digits = 0
    j = 0
    millis_digits = 0
    just_time = False
    year_sign: int | None = None
    tz: str | None = None
    if data[0] in (ord("-"), ord("+")):
        year_sign = -1 if data[0] == ord("-") else 1
        j += 1
    n = len(data)
    while j < n:
        b = data[j]
        d = b - ord("0")
        if d < 0 or d > 9:
            if j == 0 and b == ord("T"):
                just_time = True
                i += 3
            elif i < 2:
                if b == ord("-"):
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                elif i == 0 and b == ord(":") and year_sign is None:
                    just_time = True
                    if not _valid_digits(3, digits):
                        return None
                    seg[3], value, digits = value, 0, 0
                    i = 4
                else:
                    return None
            elif i == 2:
                if b in (ord(" "), ord("T")):
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                else:
                    return None
            elif i in (3, 4):
                if b == ord(":"):
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                else:
                    return None
            elif i in (5, 6):
                if b == ord(".") and i == 5:
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                else:
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                    tz = data[j:].decode("utf-8", errors="replace")
                    j = n - 1
                if i == 6 and b != ord("."):
                    i += 1
            else:
                if i < len(seg) and b in (ord(":"), ord(" ")):
                    if not _valid_digits(i, digits):
                        return None
                    seg[i], value, digits = value, 0, 0
                    i += 1
                else:
                    return None
        else:
            if i == 6:
                millis_digits += 1
            if i != 6 or digits < 6:
                value = value * 10 + d
            digits += 1
        j += 1
    if not _valid_digits(i, digits):
        return None
    seg[i] = value
    while millis_digits < 6:
        seg[6] *= 10
        millis_digits += 1
    seg[0] *= year_sign or 1
    return seg, (tz.strip() if tz is not None else None), just_time


def _zone(name: str) -> _dt.tzinfo | None:
    """``DateTimeUtils.getZoneId`` for the forms CSV data contains."""
    from zoneinfo import ZoneInfo

    if name in ("Z", "UTC", "GMT", "UT"):
        return _dt.UTC
    m = re.fullmatch(r"(?:UTC|GMT|UT)?([+-])(\d{1,2})(?::?(\d{2}))?(?::?(\d{2}))?", name)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        h, mi, sec = int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0)
        if h > 18 or mi > 59 or sec > 59:
            return None
        return _dt.timezone(sign * _dt.timedelta(hours=h, minutes=mi, seconds=sec))
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def parse_timestamp(text: str, *, today: _dt.date | None = None) -> _dt.datetime | None:
    """``DefaultTimestampFormatter.parseOptional`` in UTC → aware datetime, or ``None``.

    A time-only value (``12:30:00`` / ``T12:30``) takes today's date in
    its zone, as Spark does (``LocalDate.now(zoneId)``).
    """
    parsed = _parse_timestamp_string(text)
    if parsed is None:
        return None
    seg, tz_name, just_time = parsed
    zone = _dt.UTC if tz_name is None else _zone(tz_name)
    if zone is None:
        return None
    try:
        if just_time:
            day = today or _dt.datetime.now(zone).date()
            seg[0], seg[1], seg[2] = day.year, day.month, day.day
        local = _dt.datetime(seg[0], seg[1], seg[2], seg[3], seg[4], seg[5], seg[6])
    except (ValueError, OverflowError):
        return None
    return local.replace(tzinfo=zone).astimezone(_dt.UTC)


# ---------------------------------------------------------------------------
# Inference (CSVInferSchema)
# ---------------------------------------------------------------------------

#: Type tags: ``("null",)``, ``("int",)``, ``("long",)``, ``("decimal", p, s)``,
#: ``("double",)``, ``("timestamp",)``, ``("boolean",)``, ``("string",)``.
Type = tuple[Any, ...]

_NULL: Type = ("null",)
_STRING: Type = ("string",)


def _try_decimal(field: str) -> Type:
    value = _parse_decimal(field)
    if value is not None:
        precision, scale = _decimal_precision_scale(value)
        if scale <= 0:
            # DecimalType(p, s) throws for a negative scale (default
            # ``allowNegativeScaleOfDecimal=false``), for p > 38 and s > p;
            # ``allCatch`` then falls back to the double parser.
            if scale < 0 or precision > 38 or scale > precision:
                return _try_double(field)
            return ("decimal", precision, scale)
        return _try_double(field)
    return _try_double(field)


def _try_long(field: str) -> Type:
    return ("long",) if _parse_int(field, _LONG_MIN, _LONG_MAX) else _try_decimal(field)


def _try_int(field: str) -> Type:
    return ("int",) if _parse_int(field, _INT_MIN, _INT_MAX) else _try_long(field)


def _try_double(field: str) -> Type:
    if _parse_double(field) or field in ("NaN", "-Inf", "Inf"):
        return ("double",)
    return _try_timestamp(field)


def _try_timestamp(field: str) -> Type:
    return ("timestamp",) if parse_timestamp(field) is not None else _try_boolean(field)


def _try_boolean(field: str) -> Type:
    return ("boolean",) if _parse_boolean(field) else _STRING


def _decimal_for(t: Type) -> Type:
    return {"int": ("decimal", 10, 0), "long": ("decimal", 20, 0)}[t[0]]


_NUMERIC_ORDER = ("int", "long", "double")


def _compatible(a: Type, b: Type) -> Type | None:
    if a == b:
        return a
    if a == _NULL:
        return b
    if b == _NULL:
        return a
    # TypeCoercion.findTightestCommonType: numeric widening (no decimals).
    if a[0] in _NUMERIC_ORDER and b[0] in _NUMERIC_ORDER:
        return a if _NUMERIC_ORDER.index(a[0]) > _NUMERIC_ORDER.index(b[0]) else b
    if a == _STRING or b == _STRING:
        return _STRING
    if a[0] in ("int", "long") and b[0] == "decimal":
        return _compatible(_decimal_for(a), b)
    if a[0] == "decimal" and b[0] in ("int", "long"):
        return _compatible(a, _decimal_for(b))
    if {a[0], b[0]} == {"double", "decimal"}:
        return ("double",)
    if a[0] == "decimal" and b[0] == "decimal":
        scale = max(a[2], b[2])
        rng = max(a[1] - a[2], b[1] - b[2])
        return ("double",) if rng + scale > 38 else ("decimal", rng + scale, scale)
    return None


def infer_field(so_far: Type, field: str | None) -> Type:
    """``CSVInferSchema.inferField`` (``nullValue`` is ``""``)."""
    if field is None or field == "":
        return so_far
    kind = so_far[0]
    if kind in ("null", "int"):
        nxt = _try_int(field)
    elif kind == "long":
        nxt = _try_long(field)
    elif kind == "decimal":
        nxt = _try_decimal(field)
    elif kind == "double":
        nxt = _try_double(field)
    elif kind == "timestamp":
        nxt = _try_timestamp(field)
    elif kind == "boolean":
        nxt = _try_boolean(field)
    else:
        nxt = _STRING
    return _compatible(so_far, nxt) or _STRING


def infer_type(values: Any) -> Type:
    """Infer one column's type over ``values`` (``NullType`` → string)."""
    t = _NULL
    for v in values:
        t = infer_field(t, v)
        if t == _STRING:
            break
    return _STRING if t == _NULL else t


# ---------------------------------------------------------------------------
# Rendering (CSV writer, Spark 3.3, UTC)
# ---------------------------------------------------------------------------


def render(value: str | None, t: Type) -> str | None:
    """The CSV writer's text for ``value`` read as type ``t`` (non-strings never quote)."""
    if value is None or value == "":
        return None
    kind = t[0]
    if kind == "string":
        return value
    if kind in ("int", "long"):
        return str(int(value)) if _JAVA_INT.fullmatch(value) else None
    if kind == "decimal":
        dec = _parse_decimal(value)
        if dec is None:
            return None
        quantised = dec.quantize(Decimal(1).scaleb(-t[2])) if t[2] else dec.to_integral_value()
        return _java_bigdecimal_text(quantised, t[2])
    if kind == "double":
        return _render_double(value)
    if kind == "timestamp":
        ts = parse_timestamp(value)
        if ts is None:
            return None
        millis = ts.microsecond // 1000
        return ts.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"
    if kind == "boolean":
        return "true" if value.lower() == "true" else "false"
    return value


def _render_double(value: str) -> str | None:
    from ovid2.core.engines._duck_udf import java_double_text

    text = _java_trim(value)
    if text in ("NaN", "+NaN", "-NaN"):
        return "NaN"
    if text in ("Inf", "+Inf", "Infinity", "+Infinity"):
        return "Infinity"
    if text in ("-Inf", "-Infinity"):
        return "-Infinity"
    try:
        stripped = text[:-1] if text[-1:] in "fFdD" and not text.lower().startswith("0x") else text
        number = float.fromhex(stripped) if "x" in stripped.lower() else float(stripped)
    except (ValueError, IndexError):
        return None
    return java_double_text(number)


def _java_bigdecimal_text(value: Decimal, scale: int) -> str:
    """``java.math.BigDecimal.toString`` for a value with non-negative scale."""
    sign, digits, _exp = value.as_tuple()
    unscaled = "".join(map(str, digits)).lstrip("0") or "0"
    if scale == 0:
        text = unscaled
    else:
        unscaled = unscaled.rjust(scale + 1, "0")
        text = unscaled[:-scale] + "." + unscaled[-scale:]
    return ("-" if sign and unscaled.strip("0.") else "") + text


# ---------------------------------------------------------------------------
# Frame application
# ---------------------------------------------------------------------------


def infer_and_render(view: Any, spark: Any) -> Any:
    """Infer each column's Spark 3.3 type over ``view`` and render its writer text.

    Args:
        view: All-string frame read back from the dedup file.
        spark: Engine session (unused; kept for symmetry with the Spark path).

    Returns:
        A frame with the same columns, every cell the CSV-writer text of
        its inferred-type value. Schema inference is the one eager scan
        legacy ran too (``inferSchema`` reads the file once).
    """
    del spark
    from ovid2.core.engines import dsl
    from ovid2.core.engines.types import StringType

    columns = list(view.columns)
    positional = view.toDF(*[f"_c{i}" for i in range(len(columns))])
    types = _infer_column_types(positional, len(columns))
    from ovid2.legacy.spark33_native import native_engine, render_items

    engine = native_engine(positional)
    if engine is not None:
        # Native rendering (parallel SQL); exact Python ``render`` only for
        # the cells SQL cannot prove equivalent.
        names = [f"_c{i}" for i in range(len(columns))]
        return positional.select_native(render_items(engine, types, names)).toDF(*columns)
    out = []
    for i, t in enumerate(types):
        if t == _STRING:
            out.append(dsl.col(f"_c{i}"))
            continue
        fn = dsl.udf(lambda v, tt=t: render(v, tt), StringType())
        out.append(fn(dsl.col(f"_c{i}")).alias(f"_c{i}"))
    return positional.select(*out).toDF(*columns)


def _infer_column_types(positional: Any, n: int) -> list[Type]:
    """Infer every column's type over all rows.

    On DuckDB (native text paths on) the types are decided in one parallel
    SQL pass (:mod:`ovid2.legacy.spark33_native`); only columns whose type
    could depend on row order run the exact sequential Python fold.
    """
    from ovid2.legacy.spark33_native import infer_column_types, native_engine

    names = [f"_c{i}" for i in range(n)]
    if native_engine(positional) is not None:
        native = infer_column_types(positional, names)
        pending = [i for i, t in enumerate(native) if t is None]
        if not pending:
            return [t for t in native if t is not None]
        exact = _fold_column_types(positional.select(*[names[i] for i in pending]), len(pending))
        merged: list[Type] = []
        it = iter(exact)
        for t in native:
            merged.append(next(it) if t is None else t)
        return merged
    return _fold_column_types(positional, n)


def _fold_column_types(positional: Any, n: int) -> list[Type]:
    """The exact ``inferField`` fold over every row, in Python (reference implementation)."""
    types: list[Type] = [_NULL] * n
    for batch in _batches(positional):
        for i, col in enumerate(batch.columns):
            t = types[i]
            if t == _STRING:
                continue
            for v in col.to_pylist():
                t = infer_field(t, v)
                if t == _STRING:
                    break
            types[i] = t
        if all(t == _STRING for t in types):
            break
    return [_STRING if t == _NULL else t for t in types]


def _typed_value(value: str | None, t: Type) -> Any:
    if value is None or value == "":
        return None
    kind = t[0]
    if kind in ("int", "long"):
        return int(value) if _JAVA_INT.fullmatch(value) else None
    if kind == "decimal":
        dec = _parse_decimal(value)
        return dec
    if kind == "double":
        text = _render_double(value)
        return float(text.replace("Infinity", "inf")) if text is not None else None
    if kind == "timestamp":
        ts = parse_timestamp(value)
        return ts.replace(tzinfo=None) if ts is not None else None
    if kind == "boolean":
        return value.lower() == "true"
    return value


_SQL_TYPES: dict[str, str] = {
    "int": "int",
    "long": "bigint",
    "double": "double",
    "timestamp": "timestamp",
    "boolean": "boolean",
}


def infer_typed(view: Any) -> Any:
    """``spark.read.csv(..., inferSchema=true)`` on a non-Spark engine (typed columns, UTC)."""
    from ovid2.core.engines import dsl

    columns = list(view.columns)
    positional = view.toDF(*[f"_c{i}" for i in range(len(columns))])
    types = _infer_column_types(positional, len(columns))
    out = []
    for i, t in enumerate(types):
        if t == _STRING:
            out.append(dsl.col(f"_c{i}"))
            continue
        sql_type = f"decimal({t[1]},{t[2]})" if t[0] == "decimal" else _SQL_TYPES[t[0]]
        fn = dsl.udf(lambda v, tt=t: _typed_value(v, tt), sql_type)
        out.append(fn(dsl.col(f"_c{i}")).alias(f"_c{i}"))
    return positional.select(*out).toDF(*columns)


def _batches(df: Any) -> Any:
    to_batches = getattr(df, "to_arrow_batches", None)
    if callable(to_batches):
        yield from to_batches()
        return
    yield from df.to_arrow().to_batches()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
