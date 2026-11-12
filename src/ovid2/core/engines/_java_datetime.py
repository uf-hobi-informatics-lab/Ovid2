"""Java/Spark datetime patterns -> DuckDB SQL (pure, no engine import).

The pipeline's formatters are authored against Spark's datetime dialect
(``to_date(c, 'yyyy-MM-dd')``, ``date_format(c, 'yyyy-MM-dd HH:mm:ss')``)
and production pins ``spark.sql.legacy.timeParserPolicy=LEGACY``, i.e.
``java.text.SimpleDateFormat`` parsing. Its contract differs sharply from
DuckDB's ``strptime``:

* **Prefix match** — trailing input is ignored (``'2023-04-15abc'`` parses).
* **Variable-width digits** — a numeric field that is *not* abutting
  another numeric field consumes every digit (``'2023-4-5'`` parses with
  ``yyyy-MM-dd``; ``'4/5/23'`` with ``MM/dd/yyyy`` is year ``23``).
  Abutting numeric fields (``yyyyMMdd``) use the pattern-letter width.
* **Strict ranges** — ``2023-13-01`` / ``2023-02-30`` / hour ``24`` are
  NULL (Spark builds the formatter non-lenient).
* Missing fields default to ``1970-01-01 00:00:00``.

:func:`parse_sql` compiles a pattern into a single-pass
``regexp_extract`` (named groups -> STRUCT) followed by a range-checked
``make_timestamp``, wrapped in a ``list_transform`` lambda so the regex
runs once per row. :func:`cast_sql` reproduces the *format-less*
``to_date(c)`` / ``to_timestamp(c)`` / ``CAST(c AS DATE)`` grammar
(``yyyy[-m[m][-d[d][ T…]]]``). :func:`strftime_format` translates a
pattern for ``date_format``. The ground truth for every rule above is
``tests/unit/core/engines/test_java_datetime.py`` (recorded from Spark
4.1 with ``timeParserPolicy=LEGACY``).

ovid2: allow-sql-fstring — emits DuckDB *expression* fragments whose
only interpolated values are the caller's already-rendered column SQL
and literals escaped by :func:`sql_string`.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

__all__ = [
    "JavaPatternError",
    "cast_sql",
    "parse_sql",
    "sql_string",
    "strftime_format",
    "tokenize",
]


class JavaPatternError(ValueError):
    """Raised for a Java datetime pattern the translator cannot express."""


def sql_string(value: str) -> str:
    """Return ``value`` as a single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class _Token:
    """One lexical unit of a Java pattern: a letter run or a literal."""

    letter: str  # pattern letter, or "" for a literal
    width: int
    text: str  # literal text (for letter == "")


def tokenize(pattern: str) -> list[_Token]:
    """Split a Java ``SimpleDateFormat`` pattern into tokens.

    Letter runs (``yyyy``, ``MM``) become field tokens; quoted sections
    (``'T'``) and every non-letter character become literals. ``''``
    is a literal single quote.
    """
    tokens: list[_Token] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "'":
            if i + 1 < n and pattern[i + 1] == "'":
                tokens.append(_Token("", 0, "'"))
                i += 2
                continue
            end = pattern.find("'", i + 1)
            if end == -1:
                raise JavaPatternError(f"Unterminated quote in pattern {pattern!r}")
            tokens.append(_Token("", 0, pattern[i + 1 : end].replace("''", "'")))
            i = end + 1
            continue
        if ch.isalpha():
            j = i
            while j < n and pattern[j] == ch:
                j += 1
            tokens.append(_Token(ch, j - i, ""))
            i = j
            continue
        tokens.append(_Token("", 0, ch))
        i += 1
    return tokens


# ---------------------------------------------------------------------------
# date_format: Java pattern -> strftime
# ---------------------------------------------------------------------------


def _strftime_token(tok: _Token) -> str:
    """Return the strftime directive for one field token."""
    letter, width = tok.letter, tok.width
    if letter in ("y", "u"):
        return "%y" if width == 2 else "%Y"
    if letter in ("M", "L"):
        return {1: "%-m", 2: "%m", 3: "%b"}.get(width, "%B")
    if letter == "d":
        return "%-d" if width == 1 else "%d"
    if letter == "H":
        return "%-H" if width == 1 else "%H"
    if letter == "h":
        return "%-I" if width == 1 else "%I"
    if letter == "m":
        return "%-M" if width == 1 else "%M"
    if letter == "s":
        return "%-S" if width == 1 else "%S"
    if letter == "S":
        if width == 3:
            return "%g"
        if width == 6:
            return "%f"
        raise JavaPatternError(f"Unsupported fraction width Sx{width}")
    if letter == "a":
        return "%p"
    if letter == "E":
        return "%a" if width <= 3 else "%A"
    if letter == "D":
        return "%j"
    raise JavaPatternError(f"Unsupported pattern letter {letter!r}")


def strftime_format(pattern: str) -> str:
    """Translate a Java pattern into a DuckDB ``strftime`` format string."""
    out: list[str] = []
    for tok in tokenize(pattern):
        if tok.letter:
            out.append(_strftime_token(tok))
        else:
            out.append(tok.text.replace("%", "%%"))
    return "".join(out)


# ---------------------------------------------------------------------------
# to_date / to_timestamp with a pattern: SimpleDateFormat parse semantics
# ---------------------------------------------------------------------------

_NUMERIC_LETTERS = frozenset("yuMLdHhkKmsSD")
_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_FULL_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _is_numeric(tok: _Token) -> bool:
    if tok.letter in ("M", "L") and tok.width >= 3:
        return False
    return tok.letter in _NUMERIC_LETTERS


def _int(group: str) -> str:
    return f"TRY_CAST(NULLIF(__ovr.{group}, '') AS BIGINT)"


#: Pattern letter -> (canonical digit count, strptime directive) for the
#: :func:`_fast_path` guard.
_FAST_FIELDS: dict[str, tuple[int, str]] = {
    "y": (4, "%Y"),
    "M": (2, "%m"),
    "d": (2, "%d"),
    "H": (2, "%H"),
    "m": (2, "%M"),
    "s": (2, "%S"),
}


def _fast_path(value_sql: str, pattern: str) -> tuple[str, str] | None:
    """Return ``(guard_sql, parse_sql)`` for the canonical layout, if any.

    For a pattern made only of non-abutting ``y``/``M``/``d``/``H``/``m``/``s``
    fields (``yyyy`` 4 wide, the rest 1–2 wide) separated by single
    non-alphanumeric literals, a value whose prefix is *exactly* the
    zero-padded canonical layout (``2026-01-02 03:04:05``) followed by a
    non-digit or the end parses identically under LEGACY
    ``SimpleDateFormat`` and DuckDB's native ``try_strptime`` on that
    prefix — including the strict range checks (Feb 30, hour 24 → NULL).
    Everything else (leading spaces, 1-digit fields, trailing digits,
    other letters) takes the general regex path.
    """
    tokens = tokenize(pattern)
    regex = ["^"]
    fmt: list[str] = []
    width = 0
    prev_field = False
    for tok in tokens:
        if tok.letter:
            spec = _FAST_FIELDS.get(tok.letter)
            if spec is None or prev_field:
                return None
            if tok.letter == "y" and tok.width != 4:
                return None
            if tok.letter != "y" and tok.width > 2:
                return None
            digits, directive = spec
            regex.append(f"\\d{{{digits}}}")
            fmt.append(directive)
            width += digits
            prev_field = True
            continue
        if len(tok.text) != 1 or tok.text.isalnum() or tok.text in "%\\":
            return None
        regex.append(re.escape(tok.text))
        fmt.append(tok.text)
        width += 1
        prev_field = False
    letters = [t.letter for t in tokens if t.letter]
    # Only full dates: a missing field defaults to 1970-01-01 in Spark,
    # which strptime does not reproduce; a pattern must also *start*
    # with its year so the guard's canonical layout is unambiguous.
    if sorted(set(letters)) != sorted(letters) or not {"y", "M", "d"} <= set(letters):
        return None
    if letters[0] != "y":
        return None
    regex.append("(?:[^0-9]|$)")
    guard = f"regexp_matches(CAST({value_sql} AS VARCHAR), {sql_string(''.join(regex))})"
    parse = (
        f"CAST(try_strptime(CAST({value_sql} AS VARCHAR)[1:{width}], "
        f"{sql_string(''.join(fmt))}) AS TIMESTAMP)"
    )
    return guard, parse


def parse_sql(value_sql: str, pattern: str) -> str:
    """Return SQL parsing ``value_sql`` (VARCHAR) with a Java pattern.

    The result is a ``TIMESTAMP`` expression; NULL on no-match or on an
    out-of-range field, matching Spark's LEGACY ``to_timestamp``.

    Args:
        value_sql: Rendered DuckDB SQL for the input (must be VARCHAR).
        pattern: Java ``SimpleDateFormat`` pattern.

    Raises:
        JavaPatternError: For letters the translator does not support
            (time zones, week-based fields).
    """
    tokens = tokenize(pattern)
    regex: list[str] = ["^"]
    groups: list[str] = []
    roles: dict[str, str] = {}
    for idx, tok in enumerate(tokens):
        if not tok.letter:
            regex.append(re.escape(tok.text))
            continue
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        abutting = nxt is not None and bool(nxt.letter) and _is_numeric(nxt)
        name = f"g{len(groups)}"
        letter = tok.letter
        if _is_numeric(tok):
            digits = f"\\d{{{tok.width}}}" if abutting else "\\d+"
            regex.append(f"\\s*({digits})")
        elif letter in ("M", "L"):
            regex.append("([A-Za-z]+)")
        elif letter == "a":
            regex.append("([AaPp][Mm])")
        elif letter == "E":
            regex.append("[A-Za-z]+")
            continue
        else:
            raise JavaPatternError(f"Unsupported parse letter {letter!r} in {pattern!r}")
        groups.append(name)
        role = {
            "y": "year",
            "u": "year",
            "M": "month",
            "L": "month",
            "d": "day",
            "H": "hour",
            "k": "hour24k",
            "h": "hour12",
            "K": "hour12k",
            "m": "minute",
            "s": "second",
            "S": "millis",
            "a": "ampm",
            "D": "doy",
        }[letter]
        if role == "month" and tok.width >= 3:
            role = "month_text"
        if role == "year" and tok.width == 2:
            role = "year2"
        roles[role] = name

    if not groups:
        # A pattern with no fields: Spark parses to the epoch on a
        # literal prefix match.
        literal = sql_string("".join(regex))
        return (
            f"CASE WHEN regexp_matches({value_sql}, {literal}) "
            f"THEN TIMESTAMP '1970-01-01 00:00:00' END"
        )

    year = _year_expr(roles)
    month = _month_expr(roles)
    day = _int(roles["day"]) if "day" in roles else "1"
    hour = _hour_expr(roles)
    minute = _int(roles["minute"]) if "minute" in roles else "0"
    second = _int(roles["second"]) if "second" in roles else "0"
    millis = _int(roles["millis"]) if "millis" in roles else "0"

    checks = [
        f"{hour} BETWEEN 0 AND 23",
        f"{minute} BETWEEN 0 AND 59",
        f"{second} BETWEEN 0 AND 59",
        f"{millis} BETWEEN 0 AND 999",
    ]
    if "hour12" in roles or "hour12k" in roles:
        raw = _int(roles.get("hour12") or roles["hour12k"])
        checks.append(f"{raw} BETWEEN {'1' if 'hour12' in roles else '0'} AND 12")
    if "doy" in roles:
        base = f"try(make_timestamp({year}, 1, 1, {hour}, {minute}, {second} + {millis} / 1000.0))"
        build = f"{base} + to_days(CAST({_int(roles['doy'])} - 1 AS INTEGER))"
        checks.append(f"{_int(roles['doy'])} BETWEEN 1 AND 366")
    else:
        build = f"try(make_timestamp({year}, {month}, {day}, {hour}, {minute}, {second} + {millis} / 1000.0))"

    names_sql = "[" + ", ".join(sql_string(g) for g in groups) + "]"
    regex_sql = sql_string("".join(regex))
    body = f"CASE WHEN {' AND '.join(checks)} THEN {build} END"
    general = (
        f"list_transform([regexp_extract(CAST({value_sql} AS VARCHAR), {regex_sql}, {names_sql})], "
        f"lambda __ovr: {body})[1]"
    )
    fast = _fast_path(value_sql, pattern)
    if fast is None:
        return general
    # The canonical layout (the overwhelmingly common case) goes through
    # the native parser — ~3x cheaper than regexp_extract + lambda.
    guard, parse = fast
    return f"(CASE WHEN {guard} THEN {parse} ELSE {general} END)"


def _year_expr(roles: dict[str, str]) -> str:
    if "year" in roles:
        return _int(roles["year"])
    if "year2" in roles:
        # SimpleDateFormat ``yy``: exactly two digits -> the century that
        # puts the year within [now-80, now+20); anything else literal.
        raw = f"__ovr.{roles['year2']}"
        pivot = _dt.date.today().year + 20
        value = f"TRY_CAST(NULLIF({raw}, '') AS BIGINT)"
        return (
            f"CASE WHEN length({raw}) = 2 THEN "
            f"CASE WHEN 2000 + {value} < {pivot} THEN 2000 + {value} ELSE 1900 + {value} END "
            f"ELSE {value} END"
        )
    return "1970"


def _month_expr(roles: dict[str, str]) -> str:
    if "month" in roles:
        return _int(roles["month"])
    if "month_text" in roles:
        raw = f"lower(__ovr.{roles['month_text']})"
        whens = " ".join(
            f"WHEN {sql_string(short)} THEN {i + 1} WHEN {sql_string(full)} THEN {i + 1}"
            for i, (short, full) in enumerate(zip(_MONTHS, _FULL_MONTHS, strict=True))
        )
        return f"(CASE {raw} {whens} END)"
    return "1"


def _hour_expr(roles: dict[str, str]) -> str:
    if "hour" in roles:
        return _int(roles["hour"])
    if "hour24k" in roles:  # k: 1-24, 24 == midnight
        raw = _int(roles["hour24k"])
        return f"(CASE WHEN {raw} = 24 THEN 0 ELSE {raw} END)"
    if "hour12" in roles or "hour12k" in roles:
        raw = _int(roles.get("hour12") or roles["hour12k"])
        pm = f"upper(__ovr.{roles['ampm']}) = 'PM'" if "ampm" in roles else "false"
        return f"((CASE WHEN {raw} = 12 THEN 0 ELSE {raw} END) + CASE WHEN {pm} THEN 12 ELSE 0 END)"
    return "0"


# ---------------------------------------------------------------------------
# Format-less cast grammar (to_date(c) / to_timestamp(c) / CAST AS DATE)
# ---------------------------------------------------------------------------

_DATE_RE = r"^\s*(\d{4,6})(?:-(\d{1,2})(?:-(\d{1,2})(?:[ T].*)?)?)?\s*$"
_TS_RE = (
    r"^\s*(\d{4,6})(?:-(\d{1,2})(?:-(\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,9}))?)?)?)?)?\s*$"
)


def cast_sql(value_sql: str, *, timestamp: bool) -> str:
    """Return SQL implementing Spark's format-less string->date/timestamp cast.

    Accepts ``yyyy``, ``yyyy-[m]m``, ``yyyy-[m]m-[d]d`` (dates also take
    any ``' '``/``'T'``-prefixed suffix; timestamps take
    ``[ T]h:m[:s[.fraction]]``) surrounded by optional whitespace. Any
    other input is NULL.

    Args:
        value_sql: Rendered DuckDB SQL for the input.
        timestamp: ``True`` for a TIMESTAMP result, ``False`` for DATE.
    """
    if not timestamp:
        names = "['y', 'm', 'd']"
        build = (
            "try(make_date(TRY_CAST(__ovr.y AS BIGINT), "
            "COALESCE(TRY_CAST(NULLIF(__ovr.m, '') AS BIGINT), 1), "
            "COALESCE(TRY_CAST(NULLIF(__ovr.d, '') AS BIGINT), 1)))"
        )
        regex = sql_string(_DATE_RE)
        guard = "__ovr.y <> ''"
    else:
        names = "['y', 'm', 'd', 'h', 'mi', 's', 'f']"
        hour = "COALESCE(TRY_CAST(NULLIF(__ovr.h, '') AS BIGINT), 0)"
        minute = "COALESCE(TRY_CAST(NULLIF(__ovr.mi, '') AS BIGINT), 0)"
        second = "COALESCE(TRY_CAST(NULLIF(__ovr.s, '') AS BIGINT), 0)"
        frac = "COALESCE(TRY_CAST(NULLIF('0.' || __ovr.f, '0.') AS DOUBLE), 0)"
        build = (
            "CASE WHEN "
            f"{hour} BETWEEN 0 AND 23 AND {minute} BETWEEN 0 AND 59 AND {second} BETWEEN 0 AND 59 "
            "THEN try(make_timestamp(TRY_CAST(__ovr.y AS BIGINT), "
            "COALESCE(TRY_CAST(NULLIF(__ovr.m, '') AS BIGINT), 1), "
            "COALESCE(TRY_CAST(NULLIF(__ovr.d, '') AS BIGINT), 1), "
            f"{hour}, {minute}, {second} + {frac})) END"
        )
        regex = sql_string(_TS_RE)
        guard = "__ovr.y <> ''"
    return (
        f"list_transform([regexp_extract(CAST({value_sql} AS VARCHAR), {regex}, {names})], "
        f"lambda __ovr: CASE WHEN {guard} THEN {build} END)[1]"
    )
