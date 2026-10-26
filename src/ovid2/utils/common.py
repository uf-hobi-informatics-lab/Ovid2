"""Common utility functions.

Ported from the April 2026 refactor's ``core/utils/common.py``.
Adjusted for Ovid2:

- XDG-compliant seed file resolution (falls back to legacy
  ``/ovid_credentials/`` for parity with existing deployments).
- Type hints on every public function.
- No deprecated Python-UDF variants — only the vectorized/native paths.
"""

from __future__ import annotations

import os
from base64 import b64encode
from pathlib import Path
from typing import Any

import pandas as pd
from Crypto.Hash import SHA1
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import (
    coalesce,
    col,
    current_timestamp,
    date_format,
    regexp_extract,
    regexp_replace,
    substring,
    to_date,
    to_timestamp,
    upper,
    when,
)
from pyspark.sql.pandas.functions import pandas_udf
from pyspark.sql.types import StringType

from ovid2.config.settings import LEGACY_CREDENTIALS_DIR, SEED_FILE_NAME

# ---------------------------------------------------------------------------
# Time utilities — native Spark
# ---------------------------------------------------------------------------


def get_current_time_col() -> Any:
    """Return a Spark column expression for the current timestamp.

    Uses native ``date_format(current_timestamp(), ...)`` — no Python UDF, so
    it runs entirely in the JVM and is ~10× faster than the row-UDF variant
    used by the legacy Ovid.

    Returns:
        Column expression producing ``yyyy-MM-dd HH:mm:ss`` strings.
    """
    return date_format(current_timestamp(), "yyyy-MM-dd HH:mm:ss")


# ---------------------------------------------------------------------------
# Date formatting — native Spark
# ---------------------------------------------------------------------------


def format_date_col(column: str | Any) -> Any:
    """Return a column expression that parses common date formats.

    Coalesces ``to_date`` attempts across seven input formats, matching the
    behaviour of the original ``dateutil.parser.parse``-based UDF:

    - ``yyyy-MM-dd`` (ISO)
    - ``MM/dd/yyyy`` (US)
    - ``yyyy/MM/dd``
    - ``dd-MM-yyyy`` (EU)
    - ``yyyyMMdd`` (compact)
    - ``yyyy-MM-dd HH:mm:ss``
    - ``MM/dd/yyyy HH:mm:ss``

    Falls back to the first 10 characters when none of the above parses.

    Args:
        column: Column name or column expression.

    Returns:
        Column expression producing ``yyyy-MM-dd`` strings, or the first
        10 characters if all parsers fail.
    """
    c = col(column) if isinstance(column, str) else column
    return coalesce(
        date_format(to_date(c, "yyyy-MM-dd"), "yyyy-MM-dd"),
        date_format(to_date(c, "MM/dd/yyyy"), "yyyy-MM-dd"),
        date_format(to_date(c, "yyyy/MM/dd"), "yyyy-MM-dd"),
        date_format(to_date(c, "dd-MM-yyyy"), "yyyy-MM-dd"),
        date_format(to_date(c, "yyyyMMdd"), "yyyy-MM-dd"),
        date_format(to_date(c, "yyyy-MM-dd HH:mm:ss"), "yyyy-MM-dd"),
        date_format(to_date(c, "MM/dd/yyyy HH:mm:ss"), "yyyy-MM-dd"),
        when(c.isNotNull(), substring(c, 1, 10)),
    )


def get_time_from_datetime_col(column: str | Any) -> Any:
    """Extract ``HH:mm`` from a datetime column using native Spark functions.

    Args:
        column: Column name or column expression.

    Returns:
        Column expression producing ``HH:mm`` strings.
    """
    c = col(column) if isinstance(column, str) else column
    return coalesce(
        date_format(to_timestamp(c, "yyyy-MM-dd HH:mm:ss"), "HH:mm"),
        date_format(to_timestamp(c, "MM/dd/yyyy HH:mm:ss"), "HH:mm"),
        date_format(to_timestamp(c, "HH:mm:ss"), "HH:mm"),
        when(c.contains(":"), regexp_extract(c, r"(\d{1,2}:\d{2})", 1)),
    )


# ---------------------------------------------------------------------------
# Column-name helpers
# ---------------------------------------------------------------------------


def normalize_column_name(col_name: str) -> str:
    """Uppercase and strip a column name (used by CDM validators)."""
    return col_name.upper().strip()


def get_col_ci(df: DataFrame, column_name: str) -> Any:
    """Return a Spark column, matching ``column_name`` case-insensitively.

    Args:
        df: Spark DataFrame.
        column_name: Column name to look up.

    Returns:
        The column bound to the actual (case-preserved) DataFrame column.

    Raises:
        ValueError: If no column matches (case-insensitively).
    """
    target = column_name.lower()
    for actual in df.columns:
        if actual.lower() == target:
            return df[actual]
    raise ValueError(
        f"Column '{column_name}' not found (case-insensitive). Available: {df.columns}"
    )


def lowercase_column_names(df: DataFrame) -> DataFrame:
    """Return ``df`` with every column name lowercased."""
    return df.toDF(*[c.lower() for c in df.columns])


# ---------------------------------------------------------------------------
# Shared formatter utility functions (native Spark)
# ---------------------------------------------------------------------------


def validate_numeric_col(column: str | Any) -> Any:
    """Return the value when it is castable to ``double``, else ``null``."""
    c = col(column) if isinstance(column, str) else column
    return when(c.cast("double").isNotNull(), c).otherwise(None)


def validate_zip5_col(column: str | Any) -> Any:
    """Return a 5-digit US ZIP extracted from the input, else ``null``.

    - ``"12345"``      → ``"12345"``
    - ``"12345-6789"`` → ``"12345"`` (strips hyphen, keeps first 5)
    - ``"123456789"``  → ``"12345"`` (ZIP9 truncated to first 5)
    - anything else    → ``None``
    """
    c = col(column) if isinstance(column, str) else column
    digits = regexp_replace(c, r"[^0-9]", "")
    return (
        when(digits.rlike(r"^\d{5}$"), digits)
        .when(digits.rlike(r"^\d{9}$"), substring(digits, 1, 5))
        .otherwise(None)
    )


def validate_zip9_col(column: str | Any) -> Any:
    """Return a 9-digit ZIP (with hyphens stripped) or ``null``."""
    c = col(column) if isinstance(column, str) else column
    cleaned = regexp_replace(c, "-", "")
    return when(cleaned.rlike(r"^\d{9}$"), cleaned).otherwise(None)


_US_STATES: tuple[str, ...] = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
)


def clean_address_state_col(column: str | Any) -> Any:
    """Return the uppercased value only if it is a valid US state code."""
    c = col(column) if isinstance(column, str) else column
    upper_c = upper(c)
    return when(upper_c.isin(list(_US_STATES)), upper_c).otherwise(None)


def truncate_col(column: str | Any, length: int) -> Any:
    """Truncate a string column to the given character length."""
    c = col(column) if isinstance(column, str) else column
    return substring(c, 1, length)


def format_float_col(column: str | Any) -> Any:
    """Return the value (with commas stripped) when castable to ``double``, else ``null``."""
    c = col(column) if isinstance(column, str) else column
    cleaned = regexp_replace(c, ",", "")
    return when(cleaned.cast("double").isNotNull(), cleaned).otherwise(None)


# ---------------------------------------------------------------------------
# Encryption utilities
# ---------------------------------------------------------------------------

# Pipeline encryption value mapping (see legacy Ovid for provenance).
PIPELINE_ENCRYPTION_VALUES: dict[str, str] = {
    "BETA": "BETA",
    "ETA": "ETA",
    "THETA": "THETA",
    "TAU": "TAU",
    "ACME": "ACME",
    "IOTA": "IOTA",
    "GAMMA": "GAMMA",
    "SIGMA": "SIGMA",
    "DEMO": "DEMO",
    "LAMBDA_H": "LAMBDA_H",
    "LAMBDA_A": "LAMBDA_A",
    "LAMBDA_C": "LAMBDA_C",
    "MU": "MU",
    "SITE": "SITE",
    "NU": "NU",
    "OMICRON": "OMICRON",
    "RHO": "RHO",
    "ZETA": "ZETA",
    "DELTA": "DELTA",
    "XI": "XI",
    "omop_pipeline_1": "omop_pipeline_1",
    "omop_pipeline_plus": "omop_pipeline_plus",
    "pcornert_pipeline_1": "pcornert_pipeline_1",
    "kappa": "kappa",
    "NURSING_SITE": "SITE",
    "NURSING_BETA": "BETA",
    "TUMOR_SITE": "SITE",
}

_encryption_cache: dict[str, bytes] = {}


def _xor_encrypt_exact(plaintext: bytes, key: bytes) -> bytes:
    """Bytewise XOR-encrypt ``plaintext`` with a cyclic ``key``."""
    key_len = len(key)
    return bytes(plaintext[i] ^ key[i % key_len] for i in range(len(plaintext)))


def _seed_file_candidates() -> list[Path]:
    """Return possible seed-file locations in preference order.

    Order:
    1. ``$OVID2_SEED_FILE``  (explicit override)
    2. ``$XDG_CONFIG_HOME/ovid2/secrets.d/<seed>``
    3. ``~/.config/ovid2/secrets.d/<seed>``
    4. ``/etc/ovid2/secrets.d/<seed>``  (system install)
    5. ``LEGACY_CREDENTIALS_DIR/<seed>`` — parity with legacy Ovid.
    """
    candidates: list[Path] = []
    override = os.environ.get("OVID2_SEED_FILE")
    if override:
        candidates.append(Path(override))

    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates.append(Path(xdg_cfg) / "ovid2" / "secrets.d" / SEED_FILE_NAME)
    candidates.append(Path("/etc/ovid2/secrets.d") / SEED_FILE_NAME)
    candidates.append(Path(LEGACY_CREDENTIALS_DIR) / SEED_FILE_NAME)
    return candidates


def _get_encryption_key() -> bytes:
    """Load and cache the SHA-1 hash of the XOR seed file.

    Raises:
        FileNotFoundError: If none of the candidate seed locations exist.
    """
    if "hashing_key" in _encryption_cache:
        return _encryption_cache["hashing_key"]

    for candidate in _seed_file_candidates():
        if candidate.is_file():
            seed = candidate.read_text(encoding="utf-8").strip()
            key = SHA1.new(seed.encode("utf-8")).digest()
            _encryption_cache["hashing_key"] = key
            return key

    tried = ", ".join(str(c) for c in _seed_file_candidates())
    raise FileNotFoundError(f"Encryption seed file '{SEED_FILE_NAME}' not found in any of: {tried}")


def encrypt_id(id_value: str | None, pipeline: str, hashing_key: bytes | None = None) -> str | None:
    """Encrypt one ID value using the pipeline-scoped XOR + base64 scheme.

    Args:
        id_value: The plaintext ID.
        pipeline: Pipeline name (case-insensitive; must be in
            :data:`PIPELINE_ENCRYPTION_VALUES`).
        hashing_key: Pre-computed key. When ``None``, loaded from disk.

    Returns:
        The base64-encoded encrypted ID, or ``None`` when ``id_value`` is
        ``None``, or the raw ID when the pipeline is unknown.
    """
    if id_value is None:
        return None

    pipeline_prefix = PIPELINE_ENCRYPTION_VALUES.get(pipeline.upper())
    if pipeline_prefix is None:
        return id_value

    key = hashing_key if hashing_key is not None else _get_encryption_key()
    plaintext = f"{pipeline_prefix}{id_value}".encode()
    return b64encode(_xor_encrypt_exact(plaintext, key)).decode()


def encrypt_id_udf(pipeline: str) -> Any:
    """Return a vectorised Pandas UDF that encrypts an ID column.

    The seed and pipeline prefix are captured once at UDF-creation time and
    every batch is processed via Arrow — typically 5–20× faster than the
    row-per-call Python UDF the legacy code used.

    Args:
        pipeline: Pipeline name (case-insensitive).

    Returns:
        A Pandas UDF suitable for ``df.withColumn("PATID", udf("PATID"))``.
    """
    hashing_key = _get_encryption_key()
    pipeline_prefix = PIPELINE_ENCRYPTION_VALUES.get(pipeline.upper())
    key_len = len(hashing_key)

    # pyspark's .pyi predates the type-hint-based pandas_udf form and
    # requires ``functionType``; the call is valid at runtime (Spark >= 3.0).
    @pandas_udf(StringType())  # type: ignore[call-overload, untyped-decorator]
    def _encrypt_batch(id_series: pd.Series) -> pd.Series:
        if pipeline_prefix is None:
            return id_series.astype(object)

        results: list[str | None] = []
        for id_value in id_series:
            if id_value is None or (isinstance(id_value, float) and pd.isna(id_value)):
                results.append(None)
                continue
            plaintext = f"{pipeline_prefix}{id_value}".encode()
            encrypted = bytes(
                plaintext[i] ^ hashing_key[i % key_len] for i in range(len(plaintext))
            )
            results.append(b64encode(encrypted).decode())
        return pd.Series(results, dtype=object)

    return _encrypt_batch


# ---------------------------------------------------------------------------
# Spark temp view lookup
# ---------------------------------------------------------------------------


def get_temp_view(spark: SparkSession, table_name: str) -> DataFrame:
    """Look up a Spark temporary view by name.

    Exact case-insensitive match wins over suffix match; suffix match wins
    over nothing. Fails loudly if no view is registered.

    Args:
        spark: Active Spark session.
        table_name: View name to find.

    Returns:
        The Spark DataFrame for the matched view.

    Raises:
        RuntimeError: If no matching view is registered.
    """
    available = [t.name for t in spark.catalog.listTables() if t.isTemporary]
    target = table_name.lower()

    for name in available:
        if name.lower() == target:
            return spark.table(name)

    suffix = f"_{target}"
    for name in available:
        if name.lower().endswith(suffix):
            return spark.table(name)

    raise RuntimeError(f"Temporary view '{table_name}' not found. Available views: {available}")
