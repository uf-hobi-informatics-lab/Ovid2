"""Legacy-Ovid column UDFs, reproduced byte-for-byte (``legacy_ovid`` profile).

Legacy formatters and mappers call a small set of ``CommonFuncitons`` UDFs
(``cf.format_date_udf``, ``cf.encrypt_id_udf``, …). Their output is
defined by the exact Python code they run — notably
``dateutil.parser.parse`` for dates — so the only way to be identical is to
run *the same Python* per value. This module holds those functions and
exposes them three ways:

* :func:`legacy_udfs` — a namespace of engine-portable UDF objects bound
  to one pipeline, for the generated ``formatters_custom`` ports and the
  ``legacy_select`` mapper op (``format_date(col)``, ``encrypt_id(col)`` …);
* :func:`register_sql_functions` — registers them as SQL functions on the
  active engine (``legacy_format_date(dod)``) so declarative YAML
  projections can call them;
* the plain Python functions (``format_date`` …) for unit tests.

Engine-portable: UDFs are built through :mod:`ovid2.core.engines.dsl`, so
the same per-value Python runs on Spark, DuckDB and Snowpark.
"""

from __future__ import annotations

import re
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pyspark.sql import Column

_ZIP5_RE = re.compile(r"\d{5}")
_STRFTIME_YEAR_RE = re.compile(r"%%|%Y")


def legacy_strftime(value: Any, fmt: str) -> Any:
    """``value.strftime(fmt)`` with legacy-Ovid (Python 3.10 / glibc) ``%Y``.

    Legacy Ovid ran on CPython 3.10, whose ``strftime`` delegates to glibc:
    ``%Y`` renders the year *unpadded* (``201`` for year 201). CPython 3.13+
    zero-pads it to four digits (``0201``). Legacy feeds contain such typo
    years (``12/18/0201``), so every legacy ``strftime`` call — in these
    UDFs and in the generated formatter ports (the migrator rewrites
    ``x.strftime(f)`` to ``legacy_strftime(x, f)``) — goes through here.

    Args:
        value: Anything with a ``strftime`` method (``datetime``, ``date``,
            ``pandas.Timestamp`` …).
        fmt: The format string.

    Returns:
        The formatted string, exactly as legacy produced it.
    """
    year = getattr(value, "year", None)
    if isinstance(year, int) and 0 < year < 1000 and "%Y" in fmt:
        fmt = _STRFTIME_YEAR_RE.sub(lambda m: "%%" if m.group() == "%%" else str(year), fmt)
    return value.strftime(fmt)


# ---------------------------------------------------------------------------
# Pure-Python legacy functions (verbatim semantics)
# ---------------------------------------------------------------------------


#: Canonical ISO date / datetime (``YYYY-MM-DD[( |T)HH:MM[:SS[.f+]]]``) —
#: the shape of almost every legacy feed value. For these, ``dateutil``'s
#: result is the plain field values, so :func:`_iso_fields` answers
#: without the (≈25 µs) parser. Anything else — other layouts, time
#: zones, out-of-range fields, years < 1000 — goes to ``dateutil``.
#: Pinned against ``dateutil`` by ``tests/unit/legacy/test_legacy_udf_fast_path.py``.
_ISO_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[ T]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]{1,6})?)?)?",
    re.ASCII,
)


def _iso_fields(val: Any) -> tuple[str, str] | None:
    """``("YYYY-MM-DD", "HH:MM")`` for a canonical, valid ISO value, else ``None``."""
    if type(val) is not str:
        return None
    m = _ISO_RE.fullmatch(val)
    if m is None:
        return None
    year, month, day, hh, mm, ss = m.groups()
    y, mo, d = int(year), int(month), int(day)
    if y < 1000 or not 1 <= mo <= 12 or not _valid_day(y, mo, d):
        return None
    h = int(hh) if hh is not None else 0
    mi = int(mm) if mm is not None else 0
    if h > 23 or mi > 59 or (ss is not None and int(ss) > 59):
        return None
    return f"{year}-{month}-{day}", f"{h:02d}:{mi:02d}"


def _valid_day(year: int, month: int, day: int) -> bool:
    if 1 <= day <= 28:
        return True
    import calendar

    return 1 <= day <= calendar.monthrange(year, month)[1]


def format_date(val: Any) -> str | None:
    """Legacy ``CommonFuncitons.format_date`` — ``dateutil`` parse → ``%Y-%m-%d``."""
    fast = _iso_fields(val)
    if fast is not None:
        return fast[0]
    from dateutil import parser as dp

    try:
        parsed_date = dp.parse(val)
    except Exception:
        return None
    return str(legacy_strftime(parsed_date, "%Y-%m-%d"))


def format_time(val: Any) -> str:
    """Legacy ``format_time`` — ``dateutil`` parse → ``%H:%M``; ``''`` on failure."""
    fast = _iso_fields(val)
    if fast is not None:
        return fast[1]
    from dateutil import parser as dp

    try:
        parsed_time = dp.parse(val)
    except Exception:
        return ""
    return parsed_time.strftime("%H:%M")


def get_time_from_datetime(date_time: Any) -> str | None:
    """Legacy ``get_time_from_datetime`` — ``HH:MM`` of a datetime, else ``None``."""
    try:
        return str(date_time.time().strftime("%H:%M:%S"))[0:5]
    except Exception:
        return None


def get_date_from_datetime(date_time: Any) -> str | None:
    """Legacy ``get_date_from_datetime`` — ``%Y-%m-%d`` of a datetime, else ``None``."""
    try:
        return str(legacy_strftime(date_time, "%Y-%m-%d"))
    except Exception:
        return None


def return_if_float(val: Any) -> Any:
    """Legacy ``return_if_float`` — the value itself when ``float(val)`` works."""
    try:
        float(val)
        return val
    except Exception:
        return None


def copy_if_float(val: Any) -> float | None:
    """Legacy ``copy_if_float`` — ``float(val)`` for truthy parseable values."""
    if not val:
        return None
    try:
        return float(val)
    except Exception:
        return None


def get_valid_zip5(zip5: Any) -> str | None:
    """Legacy ``get_valid_zip5`` — the value when it is exactly five digits."""
    if isinstance(zip5, str) and _ZIP5_RE.fullmatch(zip5):
        return zip5
    return None


def current_time_new_york() -> str:
    """Legacy ``get_current_time`` — wall clock in America/New_York."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Spark UDF namespace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyUdfs:
    """UDF objects bound to one pipeline (call like functions on columns)."""

    pipeline: str
    format_date: Any
    format_time: Any
    get_time_from_datetime: Any
    get_date_from_datetime: Any
    return_if_float: Any
    copy_if_float: Any
    get_valid_zip5: Any
    encrypt_id: Any

    def current_time(self) -> Column:
        """Return the legacy ``UPDATED`` expression (New York wall clock)."""
        from ovid2.core.engines import dsl

        return cast(
            "Column",
            dsl.date_format(
                dsl.from_utc_timestamp(dsl.current_timestamp(), "America/New_York"),
                "yyyy-MM-dd HH:mm:ss",
            ),
        )

    def source(self) -> Column:
        """Return the legacy ``SOURCE`` literal (``partner.upper()``)."""
        from ovid2.core.engines import dsl

        return cast("Column", dsl.lit(self.pipeline.upper()))


def _string_udf(fn: Any) -> Any:
    """Wrap ``fn`` as a row-at-a-time string UDF (exact legacy evaluation)."""
    from ovid2.core.engines.dsl import udf
    from ovid2.core.engines.types import StringType

    return udf(fn, StringType())


def _vectorised_string_udf(fn: Any) -> Any:
    """Wrap a str→str ``fn`` as a pandas UDF (same per-value semantics, faster).

    Each batch evaluates ``fn`` once per *distinct* value (legacy date /
    time / zip columns repeat heavily), and a bounded per-process cache
    carries results across batches. ``fn`` must be pure — every function
    wrapped here is.
    """
    import pandas as pd

    from ovid2.core.engines.dsl import pandas_udf
    from ovid2.core.engines.types import StringType

    # Capture the plain function, never its ``lru_cache`` wrapper: the
    # wrapper copies ``fn``'s ``__qualname__`` so cloudpickle tries to
    # pickle it by reference and fails ("not the same object as …") when
    # PySpark ships the UDF to executors. The cache is built per process.
    def cached(v: Any) -> Any:
        return _memoised(fn)(v)

    def _apply(series):  # type: ignore[no-untyped-def]
        memo: dict[Any, Any] = {}
        out = []
        for v in series:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                v = None
            key = (type(v), v)  # 1 / 1.0 / True must not share a result
            try:
                r = memo[key]
            except KeyError:
                r = memo[key] = cached(v)
            except TypeError:  # unhashable input: evaluate directly
                r = fn(v)
            out.append(r)
        return pd.Series(out, dtype=object)

    return pandas_udf(_apply, StringType())


#: :data:`_ISO_RE` as an RE2 pattern (DuckDB ``regexp_full_match``).
_ISO_SQL_RE = (
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"([ T][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?)?"
)


def _iso_fields_sql(c: str) -> str:
    """SQL: ``c`` is a canonical, valid ISO value (exactly :func:`_iso_fields` is not ``None``)."""
    return (
        f"(regexp_full_match({c}, '{_ISO_SQL_RE}') "
        f"AND try_cast(substr({c}, 1, 4) AS INTEGER) >= 1000 "
        f"AND try_cast(substr({c}, 1, 10) AS DATE) IS NOT NULL "
        f"AND (length({c}) = 10 OR (try_cast(substr({c}, 12, 2) AS INTEGER) <= 23 "
        f"AND try_cast(substr({c}, 15, 2) AS INTEGER) <= 59 "
        f"AND (length({c}) = 16 OR try_cast(substr({c}, 18, 2) AS INTEGER) <= 59))))"
    )


class _IsoFastPath:
    """``format_date`` / ``format_time`` UDF with a native ISO fast path on DuckDB.

    The pandas UDF holds the GIL, so on DuckDB every formatter date column
    ran on one worker thread while the other 95 waited (py-spy: the
    ``_vectorised_string_udf`` batch loop on the only busy thread). For
    canonical ISO values :func:`_iso_fields` is a pure field slice, so
    DuckDB answers those in SQL and only other values reach the exact
    Python function. Non-string inputs (Python renders ints / dates
    differently) and other engines always use the UDF.
    """

    def __init__(self, udf: Any, part: str) -> None:
        self._udf = udf
        self._part = part
        self.__name__ = getattr(udf, "__name__", f"format_{part}")

    def __call__(self, column: Any) -> Any:
        from ovid2.core.engines.dsl import active_engine

        engine = active_engine()
        fallback = self._udf(column)
        if (
            engine is None
            or getattr(engine, "type_name", None) != "duckdb"
            or not _native_text_on()
        ):
            return fallback
        from ovid2.core.engines._duck_column import DuckColumn, to_column

        col = to_column(column)
        part = self._part

        def _render(ctx: Any) -> str:
            if col.kind(ctx) not in ("str", "null"):
                return str(fallback.sql(ctx))
            c = col.sql(ctx)
            if part == "date":
                fast = f"substr({c}, 1, 10)"
            else:
                fast = f"CASE WHEN length({c}) = 10 THEN '00:00' ELSE substr({c}, 12, 5) END"
            return f"(CASE WHEN {_iso_fields_sql(c)} THEN {fast} ELSE {fallback.sql(ctx)} END)"

        return DuckColumn(_render, "str")

    def __getattr__(self, name: str) -> Any:
        """Delegate UDF attributes (``func``, ``engine_udf`` …) to the wrapped UDF."""
        return getattr(self._udf, name)


def _native_text_on() -> bool:
    import os

    return os.environ.get("OVID2_DUCKDB_NATIVE_TEXT", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


#: Upper bound of distinct inputs remembered per legacy function.
_MEMO_SIZE = 1 << 18


#: Per-process ``fn -> lru_cache(fn)`` (never pickled; rebuilt on each executor).
_MEMO_CACHES: dict[Any, Any] = {}


def _memoised(fn: Any) -> Any:
    """Return this process's ``lru_cache`` over a pure legacy function.

    Thread-safe and bounded. The cache lives in a module global so UDF
    closures only reference ``fn`` (picklable by name) and each Spark
    executor builds its own cache on first use.
    """
    cache = _MEMO_CACHES.get(fn)
    if cache is None:
        cache = _MEMO_CACHES.setdefault(fn, lru_cache(maxsize=_MEMO_SIZE, typed=True)(fn))
    return cache


@lru_cache(maxsize=64)
def legacy_udfs(pipeline: str) -> LegacyUdfs:
    """Return the :class:`LegacyUdfs` namespace for ``pipeline``.

    ``format_date`` / ``format_time`` / ``get_valid_zip5`` take string
    inputs in every legacy call site, so they run vectorised. The
    ``*_from_datetime`` helpers depend on the Python type Spark hands a
    row UDF (``datetime`` for timestamps, ``str`` otherwise), so they stay
    row-at-a-time to keep that behaviour exact.
    """
    return LegacyUdfs(
        pipeline=pipeline,
        format_date=_IsoFastPath(_vectorised_string_udf(format_date), "date"),
        format_time=_IsoFastPath(_vectorised_string_udf(format_time), "time"),
        get_time_from_datetime=_string_udf(get_time_from_datetime),
        get_date_from_datetime=_string_udf(get_date_from_datetime),
        return_if_float=_string_udf(return_if_float),
        copy_if_float=_string_udf(copy_if_float),
        get_valid_zip5=_vectorised_string_udf(get_valid_zip5),
        encrypt_id=_LazyEncrypt(pipeline),
    )


class _LazyEncrypt:
    """Defer seed loading until the first ``encrypt_id(...)`` call.

    Formatters never encrypt; resolving the seed eagerly would make every
    legacy formatter depend on the XOR seed secret.
    """

    def __init__(self, pipeline: str) -> None:
        self._pipeline = pipeline
        self._udf: Any = None

    def __call__(self, column: Any) -> Any:
        if self._udf is None:
            from ovid2.utils.common import encrypt_id_udf

            self._udf = encrypt_id_udf(self._pipeline)
        return self._udf(column)


def legacy_encrypt_id(id_value: str | None, pipeline: str) -> str | None:
    """Pure-Python twin of the encrypt UDF (tests / tooling)."""
    from ovid2.utils.common import _get_encryption_key, _xor_encrypt_exact, encryption_prefix

    if id_value is None:
        return None
    plaintext = f"{encryption_prefix(pipeline)}{id_value}".encode()
    return b64encode(_xor_encrypt_exact(plaintext, _get_encryption_key())).decode()


#: SQL function name → attribute on :class:`LegacyUdfs`.
SQL_FUNCTIONS: dict[str, str] = {
    "legacy_format_date": "format_date",
    "legacy_format_time": "format_time",
    "legacy_time_from_datetime": "get_time_from_datetime",
    "legacy_date_from_datetime": "get_date_from_datetime",
    "legacy_return_if_float": "return_if_float",
    "legacy_copy_if_float": "copy_if_float",
    "legacy_valid_zip5": "get_valid_zip5",
}


def register_sql_functions(spark: Any, pipeline: str) -> None:
    """Register the legacy UDFs as SQL functions on the session's engine.

    Idempotent. Spark sessions use ``spark.udf.register``; other engines
    expose the same functions through their SQL layer via
    ``session.udf.register`` when they have one (DuckDB / Snowpark
    sessions do). ``legacy_encrypt_id`` is *not* registered: encryption is
    a mapper concern and goes through the ``legacy_select`` op so the seed
    secret is only touched by the map stage.
    """
    registrar = getattr(spark, "udf", None)
    if registrar is None:
        return
    namespace = legacy_udfs(pipeline)
    for sql_name, attr in SQL_FUNCTIONS.items():
        registrar.register(sql_name, getattr(namespace, attr).engine_udf())
