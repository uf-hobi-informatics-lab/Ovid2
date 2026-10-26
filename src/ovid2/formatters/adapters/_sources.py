"""Shared adapter helpers.

Reads a pipeline-scoped source described by :class:`SourceSpec` and
returns a lazy DataFrame with predictable column-name casing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ovid2.formatters.spec import SourceSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.drivers.disc import DiscAddress

logger = logging.getLogger(__name__)


def read_source(
    source: SourceSpec,
    *,
    spark: SparkSession,
    input_address: BaseAddress,
    pipeline: str,
    batch_id: str,
    engine: BaseEngine | None = None,
    input_address_name: str | None = None,
) -> DataFrame:
    """Read one source block into a lazy DataFrame.

    Steps:

    1. Read the source. On the Spark path this delegates straight to the
       input address (disc / Snowflake / …). When ``engine`` is supplied
       and is **not** Spark-compatible (Snowpark), the read routes through
       the engine's stager instead — ``input_address_name`` is resolved to
       a native :class:`Driver` and materialised into an engine-native
       DataFrame, so the JVM-less session never hits
       ``spark.read.format('snowflake')``.
    2. Apply optional glob exclusions on file basenames (disc only).
    3. Lowercase column names when requested (default).
    4. Apply the ``filter`` predicate.

    Args:
        source: The source-spec block.
        spark: Active engine session.
        input_address: Where to read the file from (Spark path).
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        engine: Active compute engine. When non-Spark, drives the stager
            read path. ``None`` (default) keeps the classic Spark path.
        input_address_name: Address config name — required for the
            non-Spark stager read path.

    Returns:
        A lazy DataFrame with the source columns.

    Raises:
        FileNotFoundError: When the source is not optional and no files
            match the glob.
    """
    use_stager = engine is not None and not getattr(engine, "session_is_spark", True)
    try:
        if use_stager:
            df = _read_via_stager(
                source,
                engine=engine,  # type: ignore[arg-type]
                pipeline=pipeline,
                batch_id=batch_id,
                input_address_name=input_address_name,
            )
        else:
            df = input_address.read(
                spark,
                pipeline,
                batch_id,
                source.glob,
                table_name=source.name,
            )
    except FileNotFoundError:
        if source.optional:
            logger.info(
                "Optional source '%s' skipped: no files matched glob '%s'",
                source.name,
                source.glob,
            )
            return _empty_df(spark)
        raise

    # Case-insensitive exclusions on the resolved *base name* of every file
    # the DataFrame is reading. This works on disc addresses only; other
    # address types return a DataFrame with no exposed input path, so
    # excludes silently become a no-op there.
    if source.exclude and not use_stager:
        df = _apply_exclude_patterns(df, patterns=source.exclude, input_address=input_address)

    if source.lowercase_columns:
        df = df.toDF(*[c.lower() for c in df.columns])

    if source.filter:
        df = df.filter(source.filter)

    return cast("DataFrame", df)


def _read_via_stager(
    source: SourceSpec,
    *,
    engine: BaseEngine,
    pipeline: str,
    batch_id: str,
    input_address_name: str | None,
) -> Any:
    """Materialise a source through the engine's stager (non-Spark path)."""
    if not input_address_name:
        raise ValueError(
            "read_source: a non-Spark engine reads through the stager and "
            "needs the input address name, but 'input_address_name' was not "
            "supplied. Thread ctx.input_address_name through the adapter "
            "context (run_pipeline / load_formatter do this automatically)."
        )
    from ovid2.drivers.registry import driver_from_address

    driver = driver_from_address(input_address_name, pipeline=pipeline)
    stager = engine.get_stager()
    return stager.materialise(
        driver,
        pipeline,
        batch_id,
        source.glob,
        table_name=source.name,
    )


def _apply_exclude_patterns(
    df: DataFrame,
    *,
    patterns: tuple[str, ...],
    input_address: BaseAddress,
) -> DataFrame:
    """Best-effort exclusion: filters rows by ``input_file_name()`` when
    the reader supports it.

    For disc addresses, Spark's ``input_file_name()`` gives the source
    URI per row, so we can drop rows whose basename matches an exclude
    pattern. For other address types this returns an empty string, so
    the exclusion silently becomes a no-op (documented behaviour).
    """
    from pyspark.sql.functions import expr, input_file_name

    # Only meaningful for filesystem-based sources.
    disc_address = _maybe_disc(input_address)
    if disc_address is None:
        logger.info(
            "Exclude patterns %s ignored: input address does not expose file names.",
            patterns,
        )
        return df

    # Build a predicate: keep rows whose basename matches NONE of the patterns.
    # We compare on a normalised lowercase basename to align with the
    # pipeline-varying case behaviour we already handle elsewhere.
    df_with_source = df.withColumn("__ovid2_source_file__", input_file_name())
    keep_expression_parts = []
    for pattern in patterns:
        # Translate the shell glob to a Java-compatible (java.util.regex)
        # anchored regex. We deliberately do NOT use ``fnmatch.translate``:
        # since Python 3.11 it emits atomic-group emulation with named
        # groups ``(?P<g0>...)`` and backreferences ``(?P=g0)`` which Spark's
        # ``rlike`` (backed by java.util.regex) cannot parse — it raises
        # ``PatternSyntaxException: Unknown inline modifier``.
        regex = _glob_to_java_regex(pattern)
        keep_expression_parts.append(
            f"NOT (lower(reverse(split(reverse(__ovid2_source_file__), '/')[0])) rlike '{regex}')"
        )
    predicate = " AND ".join(keep_expression_parts)
    return df_with_source.filter(expr(predicate)).drop("__ovid2_source_file__")


def _maybe_disc(input_address: BaseAddress) -> DiscAddress | None:
    """Return ``input_address`` if it is a :class:`DiscAddress`, else ``None``."""
    from ovid2.drivers.disc import DiscAddress

    return input_address if isinstance(input_address, DiscAddress) else None


def _empty_df(spark: SparkSession) -> DataFrame:
    """Return a zero-column, zero-row DataFrame usable as a passthrough."""
    return spark.createDataFrame([], schema="__ovid2_empty__ string").drop("__ovid2_empty__")


def _glob_to_java_regex(pattern: str) -> str:
    """Translate a shell glob into an anchored, Java-compatible regex string.

    The result is intended for Spark's ``rlike`` (backed by
    ``java.util.regex.Pattern``) embedded inside a single-quoted SQL
    expression. Only the two shell wildcards Ovid2 uses in ``exclude``
    patterns are special:

    - ``*`` matches any run of characters (``.*``).
    - ``?`` matches exactly one character (``.``).

    Every other character is regex-escaped so metacharacters in file
    names (``.``, ``+``, ``(`` …) are treated literally. The pattern is
    lowercased to match the lowercased basename the caller compares
    against (documented case-insensitive behaviour). Single quotes are
    doubled so the regex is safe to embed in a single-quoted SQL string.

    Args:
        pattern: A shell glob such as ``*provider_sup*``.

    Returns:
        A fully-anchored Java regex string, e.g. ``^.*provider_sup.*$``.
    """
    out: list[str] = []
    for char in pattern.lower():
        if char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        elif char == "'":
            # Escape for the single-quoted SQL string literal.
            out.append("''")
        elif char.isalnum() or char == "_":
            out.append(char)
        else:
            # Regex-escape everything else so metacharacters stay literal.
            out.append("\\" + char)
    return "^" + "".join(out) + "$"
