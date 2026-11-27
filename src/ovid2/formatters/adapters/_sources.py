"""Shared adapter helpers.

Reads a pipeline-scoped source described by :class:`SourceSpec` and
returns a lazy DataFrame with predictable column-name casing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ovid2.core.sources import SourceHandle
from ovid2.formatters.spec import SourceSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine

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
    source_handle: SourceHandle | None = None,
) -> DataFrame:
    """Read one source block into a lazy DataFrame.

    Steps:

    1. Read the source through the run's :class:`SourceHandle` (native
       data plane or the engine's stager — the handle decides).
    2. Apply optional glob exclusions on file basenames (handles with
       ``is_local_path`` only).
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
        source_handle: The pipeline-built :class:`~ovid2.core.sources.SourceHandle`
            (``ctx.source``). When given, the other endpoint arguments are
            ignored; otherwise a handle is assembled from them. Storage
            resolution (driver lookup, stager vs. native plane) lives on the
            handle, never here.

    Returns:
        A lazy DataFrame with the source columns.

    Raises:
        FileNotFoundError: When the source is not optional and no files
            match the glob.
    """
    handle = source_handle or SourceHandle(
        session=spark,
        input_address=input_address,
        pipeline=pipeline,
        batch_id=batch_id,
        engine=engine,
        input_address_name=input_address_name,
    )
    try:
        df = handle.read(source.glob, table_name=source.name)
    except FileNotFoundError:
        if source.optional:
            logger.info(
                "Optional source '%s' skipped: no files matched glob '%s'",
                source.name,
                source.glob,
            )
            return _empty_df(handle.session)
        raise

    # Case-insensitive exclusions on the resolved *base name* of every file
    # the DataFrame is reading. Only handles whose rows expose a local file
    # path support it; elsewhere excludes are a documented no-op.
    if source.exclude:
        if handle.is_local_path:
            df = _apply_exclude_patterns(df, patterns=source.exclude)
        elif not handle.uses_stager:
            logger.info(
                "Exclude patterns %s ignored: input address does not expose file names.",
                source.exclude,
            )

    if source.lowercase_columns:
        df = df.toDF(*[c.lower() for c in df.columns])

    if source.filter:
        df = df.filter(source.filter)

    return cast("DataFrame", df)


def _apply_exclude_patterns(
    df: DataFrame,
    *,
    patterns: tuple[str, ...],
) -> DataFrame:
    """Best-effort exclusion: filters rows by ``input_file_name()`` when
    the reader supports it.

    For disc addresses, Spark's ``input_file_name()`` gives the source
    URI per row, so we can drop rows whose basename matches an exclude
    pattern. For other address types this returns an empty string, so
    the exclusion silently becomes a no-op (documented behaviour).
    """
    from ovid2.core.engines.dsl import expr, input_file_name

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


#: Column carried by an "absent optional source" frame on engines that
#: cannot represent a zero-column frame (DuckDB).
EMPTY_SOURCE_SENTINEL = "__ovid2_empty__"


def _empty_df(spark: SparkSession) -> DataFrame:
    """Return a zero-row DataFrame marking an absent optional source.

    Spark yields a zero-column frame. Engines that cannot drop every
    column (DuckDB) keep the single :data:`EMPTY_SOURCE_SENTINEL`
    column; :func:`is_empty_source` recognises both shapes.
    """
    df = spark.createDataFrame([], schema=f"{EMPTY_SOURCE_SENTINEL} string")
    try:
        return df.drop(EMPTY_SOURCE_SENTINEL)
    except ValueError:
        return df


def is_empty_source(df: Any) -> bool:
    """Return whether ``df`` is an absent-optional-source placeholder."""
    columns = list(df.columns)
    return not columns or columns == [EMPTY_SOURCE_SENTINEL]


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
