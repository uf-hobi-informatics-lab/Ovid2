"""Shared helpers for the PCORnet -> OMOP converter formatters.

All helpers take the engine's column-expression DSL
(:class:`~ovid2.core.engines.protocols.EngineFunctions`, obtained from
``self.context.functions``) rather than importing
``pyspark.sql.functions`` directly, so every clinical formatter runs
unchanged under PySpark and Snowpark.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineFunctions

# OMOP sentinel date for un-parseable / missing PCORnet dates (matches the
# legacy converter's ``get_*_with_default_value`` UDFs).
DEFAULT_DATE = "1900-01-01"
DEFAULT_DATETIME = "1900-01-01 00:00:00"


def hash_key_id(functions: "EngineFunctions", *column_names: str) -> Any:
    """Return a deterministic non-negative integer surrogate id.

    M2 dimension / patient-timeline tables (``location``, ``care_site``,
    ``provider``, ``observation_period``) have no natural PCORnet primary
    key, so their OMOP surrogate ids are derived by hashing the composing
    natural key (the team decision recorded in the M2 CHANGELOG entry —
    ``hash_id`` of the natural key, matching the legacy converter's
    ``concat(...)`` mapping-table key idea without the mapping-table
    round-trip).

    Unlike the generic ``hash_id`` mapper op (which emits a salted
    ``sha256`` *hex* string), OMOP surrogate ids must be numeric
    (``RDBMS Number``). This uses the engine's portable
    :meth:`~ovid2.core.engines.protocols.EngineFunctions.stable_hash`
    (PySpark 64-bit ``xxhash64`` masked to 63 bits; Snowpark
    ``abs(hash(...))``) rendered as a decimal string. Deterministic across
    runs and engines (no salt secret required).

    Null / empty components are coalesced to the empty string so a missing
    part still yields a stable id (matching the legacy ``concat`` which
    treated nulls as empty).

    Args:
        functions: The engine's column-expression DSL.
        *column_names: The source column(s) forming the natural key.

    Returns:
        A column of decimal-string non-negative integers.
    """
    col = functions.col
    lit = functions.lit
    parts = [functions.coalesce(col(name).cast("string"), lit("")) for name in column_names]
    return functions.stable_hash(*parts)


def literal_map(functions: "EngineFunctions", mapping: dict[str, str], key_col: Any) -> Any:
    """Map ``key_col`` through a ``{code: value}`` dict, portably.

    Replaces the legacy ``create_map([...])[key]`` indexing idiom (which
    Snowpark cannot express) with the engine's portable
    :meth:`~ovid2.core.engines.protocols.EngineFunctions.map_from_dict`
    (chained ``when(...).otherwise(...)``). Returns null for unmatched
    keys, matching the ``create_map`` lookup semantics.

    Args:
        functions: The engine's column-expression DSL.
        mapping: The ``{code: value}`` translation dict.
        key_col: The column expression to look up.
    """
    return functions.map_from_dict(mapping, key_col)


def default_date(functions: "EngineFunctions", column_name: str) -> Any:
    """Return ``to_date(col, 'yyyy-MM-dd')`` with the OMOP sentinel fallback."""
    col = functions.col
    return functions.coalesce(
        functions.to_date(col(column_name), "yyyy-MM-dd"),
        functions.to_date(functions.lit(DEFAULT_DATE), "yyyy-MM-dd"),
    )


def default_datetime(functions: "EngineFunctions", column_name: str) -> Any:
    """Return a ``yyyy-MM-dd HH:mm:ss`` string with the OMOP sentinel fallback."""
    col = functions.col
    return functions.coalesce(
        functions.date_format(
            functions.to_date(col(column_name), "yyyy-MM-dd"), "yyyy-MM-dd 00:00:00"
        ),
        functions.lit(DEFAULT_DATETIME),
    )


def default_datetime_with_time(
    functions: "EngineFunctions", date_column: str, time_column: str
) -> Any:
    """Combine a date + a ``HH:mm:ss`` time into a ``yyyy-MM-dd HH:mm:ss`` string.

    Mirrors the legacy ``get_datetime_from_date_and_time_with_default_value_udf``:
    the date part is parsed with the OMOP sentinel fallback, and the time
    part is appended when present (defaulting to ``00:00:00`` when the
    time column is null / empty).
    """
    col = functions.col
    lit = functions.lit
    date_part = functions.coalesce(
        functions.date_format(functions.to_date(col(date_column), "yyyy-MM-dd"), "yyyy-MM-dd"),
        lit(DEFAULT_DATE),
    )
    time_part = functions.coalesce(
        _nonempty(functions, col(time_column)),
        lit("00:00:00"),
    )
    return functions.concat(date_part, lit(" "), time_part)


def default_time(functions: "EngineFunctions", column_name: str) -> Any:
    """Return the source time string, or an empty string when missing."""
    col = functions.col
    return functions.coalesce(_nonempty(functions, col(column_name)), functions.lit(""))


def _nonempty(functions: "EngineFunctions", column: Any) -> Any:
    """Return the column value when non-null / non-empty, else null."""
    trimmed = functions.trim(column.cast("string"))
    return functions.when(trimmed == "", None).otherwise(trimmed)
