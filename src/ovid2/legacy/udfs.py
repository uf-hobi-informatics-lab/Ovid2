"""Legacy-Ovid column UDFs, reproduced byte-for-byte (``legacy_ovid`` profile).

Legacy formatters and mappers call a small set of ``CommonFuncitons`` UDFs
(``cf.format_date_udf``, ``cf.encrypt_id_udf``, …). Their output is
defined by the exact Python code they run — notably
``dateutil.parser.parse`` for dates — so the only way to be identical is to
run *the same Python* per value. This module holds those functions and
exposes them three ways:

* :func:`legacy_udfs` — a namespace of Spark UDF objects bound to one
  pipeline, for hand-ported ``formatters_custom`` classes and the
  ``legacy_select`` mapper op (``format_date(col)``, ``encrypt_id(col)`` …);
* :func:`register_sql_functions` — registers them as Spark SQL functions
  (``legacy_format_date(dod)``) so declarative YAML projections can call
  them;
* the plain Python functions (``format_date`` …) for unit tests.

Only the engine layer may import pyspark (``test_no_pyspark_outside_engine``);
this file is on that allow-list.
"""

from __future__ import annotations

import re
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import Column, SparkSession

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


def format_date(val: Any) -> str | None:
    """Legacy ``CommonFuncitons.format_date`` — ``dateutil`` parse → ``%Y-%m-%d``."""
    from dateutil import parser as dp

    try:
        parsed_date = dp.parse(val)
    except Exception:
        return None
    return str(legacy_strftime(parsed_date, "%Y-%m-%d"))


def format_time(val: Any) -> str:
    """Legacy ``format_time`` — ``dateutil`` parse → ``%H:%M``; ``''`` on failure."""
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
        from pyspark.sql import functions as sf

        return sf.date_format(
            sf.from_utc_timestamp(sf.current_timestamp(), "America/New_York"),
            "yyyy-MM-dd HH:mm:ss",
        )

    def source(self) -> Column:
        """Return the legacy ``SOURCE`` literal (``partner.upper()``)."""
        from pyspark.sql import functions as sf

        return sf.lit(self.pipeline.upper())


def _string_udf(fn: Any) -> Any:
    """Wrap ``fn`` as a row-at-a-time string UDF (exact legacy evaluation)."""
    from pyspark.sql.functions import udf
    from pyspark.sql.types import StringType

    return udf(fn, StringType())


def _vectorised_string_udf(fn: Any) -> Any:
    """Wrap a str→str ``fn`` as a pandas UDF (same per-value semantics, faster)."""
    import pandas as pd
    from pyspark.sql.functions import PandasUDFType  # type: ignore[attr-defined]
    from pyspark.sql.pandas.functions import pandas_udf
    from pyspark.sql.types import StringType

    def _apply(series):  # type: ignore[no-untyped-def]
        return pd.Series(
            [fn(None if v is None or (isinstance(v, float) and pd.isna(v)) else v) for v in series],
            dtype=object,
        )

    # Explicit eval type: string annotations (``from __future__ import
    # annotations``) defeat PySpark's type-hint inference.
    return pandas_udf(_apply, StringType(), PandasUDFType.SCALAR)


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
        format_date=_vectorised_string_udf(format_date),
        format_time=_vectorised_string_udf(format_time),
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


def register_sql_functions(spark: SparkSession, pipeline: str) -> None:
    """Register the legacy UDFs as Spark SQL functions on ``spark``.

    Idempotent. ``legacy_encrypt_id`` is *not* registered: encryption is a
    mapper concern and goes through the ``legacy_select`` op so the seed
    secret is only touched by the map stage.
    """
    namespace = legacy_udfs(pipeline)
    for sql_name, attr in SQL_FUNCTIONS.items():
        spark.udf.register(sql_name, getattr(namespace, attr))
