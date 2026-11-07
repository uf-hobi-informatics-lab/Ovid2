"""Legacy-Ovid stage-boundary CSV round-trip emulation (``legacy_ovid`` profile).

Legacy Ovid ran every ETL stage as a separate process that wrote a
tab-delimited CSV and let the next stage read it back::

    formatter ──W──▶ formatted_<t>.csv ──awk dedup (text)──▶ deduplicated_<t>.csv
        ──R──▶ mapper ──W──▶ mapped_<t>.csv ──R──▶ fix chain ──W──▶ fixed_<t>.csv

Ovid2 runs the same chain as one lazy plan, so the *side effects* of those
round-trips must be reproduced explicitly to get byte-identical output.
They were measured on the legacy image (Spark 3.3) and on the Ovid2 image
(Spark 4.1) with the exact writer/reader options both use:

* **W — write** (``quote=""``, ``nullValue=None``; univocity defaults
  ``ignoreLeading/TrailingWhiteSpace=true``): every value is rendered as
  text by Spark's CSV value formatter (``1.0E10``, ``true``, ISO
  timestamps, …) and leading / trailing characters ``<= U+0020`` are
  stripped. Nulls are written as an empty field; empty strings as ``""``.
  :func:`legacy_write_normalise` reproduces this with Spark's own
  ``to_csv`` per column — the identical univocity writer — which was
  verified byte-for-byte against real ``DataFrame.write.csv`` output on
  Spark 3.3 (legacy image) and Spark 4.1 (Ovid2) for string, double,
  decimal, int, boolean, date and timestamp columns.
* **R — read** (``header=true``, ``quote='"'``, ``escape='"'``,
  ``inferSchema=false``): both the empty field and ``""`` come back as
  ``null``; a cell wrapped in quotes is unquoted; every other field is the
  written text. A line that is blank after Java ``trim`` (an all-null /
  all-empty row) is dropped.
* **W is not idempotent** on text that holds its own quote char (``\x00``,
  used when a value contains the delimiter) or backslashes: each W escapes
  them again. So a frame that already *is* file text (the dedup stage
  output) carries :data:`LEGACY_TEXT_KEY` and is only ever R-ed or written
  raw (:func:`write_legacy_text`).

Between W and R the legacy deduplicator works on the *text* (``awk``), so
an empty string (``""``) and a null (empty field) are distinct keys
there — which is why the legacy deduplicator runs on the W-normalised
frame, before R.

First-column subtlety: univocity NUL-quotes a value that *starts a line*
with its comment char ``#`` (``\x00#…\x00``). That only happens in the
first column of the file, so only the first column is rendered with the
default comment char (verified on both Spark versions).

Known, documented non-emulated corner cases (the parity harness reports
them if they ever occur): a value containing the delimiter or a newline
(the legacy writer emits it raw and the reader re-splits the row — the
row is effectively corrupted in legacy), and values that start with
``"`` (the legacy reader treats them as quoted).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

#: Writer options identical to the legacy ``write_pyspark_output_file``.
_WRITE_OPTIONS: dict[str, str] = {"sep": "\t", "quote": ""}

#: Options for non-first columns: a value there never starts a line, so the
#: comment char must not trigger quoting (``\x01`` never appears in data).
_MID_LINE_OPTIONS: dict[str, str] = {"sep": "\t", "quote": "", "comment": "\u0001"}

#: Reader options identical to the legacy ``spark_read`` (per-cell parse).
_READ_OPTIONS: dict[str, str] = {"sep": "\t", "quote": '"', "escape": '"'}

#: Legacy reader dialects. ``cf.spark_read`` (every mapper but one) sets
#: ``escape='"'``; a direct ``spark.read.load(..., quote='"')`` (pcornet
#: ``pro_cm_mapper``) keeps Spark's default backslash escape, under which
#: ``""`` inside a quoted field does *not* escape a quote.
READER_OPTIONS: dict[str, dict[str, str]] = {
    "spark_read": _READ_OPTIONS,
    "backslash_escape": {"sep": "\t", "quote": '"'},
}

#: A line the CSV reader skips (Java ``String.trim()`` leaves nothing).
_BLANK_LINE_RE: str = "^[\\x00-\\x20]*$"

#: Token the CSV writer emits for an empty string with quoting disabled.
_EMPTY_STRING_TOKEN: str = '""'  # noqa: S105 — CSV empty-string token, not a secret

#: Pipeline-context flag: the frame in flight is already legacy file *text*
#: (the dedup stage's ``deduplicated_<t>.csv``). The next consumer must apply
#: only the reader side (R) — or, when it is the last stage, write the text
#: raw — never W again: W is not idempotent on text that contains the
#: writer's quote char (``\x00``) or backslashes.
LEGACY_TEXT_KEY: str = "legacy_ovid.frame_is_text"

#: Reader-view frame (``id``) → the file text it was read from. Lets the
#: ``legacy_select`` op re-read the *file* (``append_additional_fields``)
#: without threading the text frame through the generic mapper engine.
_SOURCE_TEXT: dict[int, DataFrame] = {}


def remember_source_text(reader_view: DataFrame, text: DataFrame) -> DataFrame:
    """Record that ``reader_view`` is ``R(text)``; returns ``reader_view``."""
    _SOURCE_TEXT.clear()  # one table in flight at a time (legacy runner)
    _SOURCE_TEXT[id(reader_view)] = text
    return reader_view


def source_text(reader_view: DataFrame) -> DataFrame:
    """Return the file text ``reader_view`` was read from (W of it if unknown)."""
    text = _SOURCE_TEXT.get(id(reader_view))
    return text if text is not None else legacy_write_normalise(reader_view)


def legacy_write_normalise(df: DataFrame) -> DataFrame:
    """Emulate the legacy CSV *writer*: every column → its written text.

    Args:
        df: Any DataFrame.

    Returns:
        A frame with the same column names, every column a string holding
        exactly the text the legacy writer would emit for that cell
        (``'""'`` for an empty string); nulls stay null.
    """
    from pyspark.sql import functions as sf

    if not df.columns:
        return df
    first, *rest = df.columns

    def _render(name: str, options: dict[str, str]) -> Any:
        return (
            sf.when(sf.col(f"`{name}`").isNull(), sf.lit(None).cast("string"))
            .otherwise(sf.to_csv(sf.struct(sf.col(f"`{name}`")), options))
            .alias(name)
        )

    # univocity's comment character (``#``) is NUL-quoted only when it opens
    # a *line* — i.e. in the first column. Other columns are rendered with
    # an unused comment char so ``#`` stays literal, exactly like the file.
    return df.select(
        _render(first, _WRITE_OPTIONS), *[_render(name, _MID_LINE_OPTIONS) for name in rest]
    )


def legacy_read_normalise(df: DataFrame, *, reader: str = "spark_read") -> DataFrame:
    """Emulate the legacy CSV *reader* on W-text — by actually reading it.

    The W-text is written verbatim as a scratch file (:func:`write_legacy_text`)
    and read back with the legacy reader options (``READER_OPTIONS[reader]``).
    This is exact by construction — every reader effect (``""``/empty →
    null, ``"…"`` unquoting, re-splitting a NUL-quoted tab value, dropping
    blank lines, safe header names) comes from Spark's own file reader —
    and, unlike an in-plan ``from_csv`` over a wide struct, it keeps
    planning linear in the column count (tumor_date has 315 columns).
    The ``legacy_ovid`` profile is eager by design (one scratch write per
    stage boundary, exactly like legacy).

    Args:
        df: A W-text frame (:func:`legacy_write_normalise` output, or the
            dedup stage's file text).
        reader: ``"spark_read"`` (``cf.spark_read``) or ``"backslash_escape"``
            (a direct ``spark.read.load(..., quote='"')``).

    Returns:
        The frame as the legacy reader saw it (header names made safe).
    """
    import tempfile

    from ovid2.config.settings import WORK_DIR

    if not df.columns:
        return df
    spark = df.sparkSession
    scratch = tempfile.mkdtemp(prefix="legacy_rt_", dir=str(WORK_DIR))
    # Scratch lives under WORK_DIR and must outlive the lazy read plan; it is
    # removed with the run's work directory.
    write_legacy_text(df, scratch, "t.csv")
    opts = dict(READER_OPTIONS[reader])
    return spark.read.csv(
        scratch + "/t.csv",
        header=True,
        inferSchema=False,
        multiLine=False,
        **opts,
    )


def safe_header(columns: list[str]) -> list[str]:
    """Column names exactly as Spark's CSV reader derives them from a header.

    Mirrors ``CSVHeaderChecker`` / ``makeSafeHeader`` (default options,
    ``spark.sql.caseSensitive=false``): an empty name becomes ``_c<i>``
    and every name that occurs more than once (case-insensitively) becomes
    ``<name><i>`` (``i`` is the 0-based position).

    Args:
        columns: Header names in file order.

    Returns:
        The names a legacy ``spark_read`` of that header would expose.
    """
    lowered = [c.lower() for c in columns if c]
    dupes = {c for c in lowered if lowered.count(c) > 1}
    return [
        f"_c{i}" if not c else (f"{c}{i}" if c.lower() in dupes else c)
        for i, c in enumerate(columns)
    ]


def legacy_round_trip(df: DataFrame) -> DataFrame:
    """Apply W then R — one full legacy stage boundary."""
    return legacy_read_normalise(legacy_write_normalise(df))


def write_legacy_text(
    df: DataFrame, folder: str, output_file_name: str, *, header: bool = True
) -> None:
    """Write a W-text frame verbatim as one tab-delimited legacy file.

    Each cell already holds the exact text of the legacy file (see
    :func:`legacy_write_normalise`); nulls are empty fields. The rows are
    joined with tabs and written with Spark's *text* writer, so no value is
    escaped or quoted a second time.

    Args:
        df: A W-text frame (e.g. the legacy dedup stage output).
        folder: Destination directory.
        output_file_name: File name (``deduplicated_<t>.csv``, ``<t>.csv`` …).
        header: Write the header line (default) — ``False`` for scratch bodies.
    """
    import glob
    import os
    import shutil

    from pyspark.sql import functions as sf

    os.makedirs(folder, exist_ok=True)
    final_path = os.path.join(folder, output_file_name)
    work = os.path.join(folder, f".tmp_{output_file_name}")
    partial = final_path + ".partial"
    header_names = list(df.columns)
    # Positional rename: legacy headers may repeat a name or be empty.
    positional = df.toDF(*[f"_p{i}" for i in range(len(header_names))])
    line = sf.concat_ws(
        "\t", *[sf.coalesce(sf.col(c), sf.lit("")) for c in positional.columns]
    ).alias("value")
    try:
        positional.select(line).write.mode("overwrite").option("lineSep", "\n").text(work)
        with open(partial, "wb") as out:
            if header:
                out.write(("\t".join(header_names) + "\n").encode("utf-8"))
            for part in sorted(glob.glob(os.path.join(work, "part-*"))):
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, out)
        os.replace(partial, final_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(partial):
            os.remove(partial)


def legacy_infer_schema_view(text: DataFrame, spark: Any) -> DataFrame:
    """Return the deduplicated file as legacy ``append_additional_fields`` re-read it.

    Legacy ``cf.append_additional_fields`` re-read ``deduplicated_<t>.csv``
    with ``inferSchema="true"`` (``quote='"'``, default backslash escape)
    and joined those *values* onto the mapped frame; the mapper's writer
    then rendered them. So appended columns carry inferred-type text:
    ``2026-06-05`` → timestamp → ``2026-06-05T00:00:00.000Z``, ``007`` →
    ``7``, ``1.50`` → ``1.5``. Spark 3.3 had no ``preferDate`` (dates infer
    as timestamps) and the legacy JVM ran in UTC.

    Reproduced exactly: the file is re-created byte for byte from its W-text
    (``text``), read back with the legacy options in UTC, string columns are
    kept as values and every non-string column becomes the writer's UTC
    text for it (no quoting can apply to those). The result is written to a
    Parquet scratch file *under UTC* (so the rendering cannot pick up the
    session time zone later) and returned lazily from there.

    Args:
        text: The ``deduplicated_<t>.csv`` **text** (W-text frame).
        spark: Active Spark session.

    Returns:
        A frame with the reader's safe header names; values as legacy saw them.
    """
    import tempfile

    from pyspark.sql import functions as sf
    from pyspark.sql.types import StringType

    from ovid2.config.settings import WORK_DIR

    scratch = tempfile.mkdtemp(prefix="legacy_infer_", dir=str(WORK_DIR))
    # Scratch lives under WORK_DIR (``legacy_infer_*``) and must outlive the
    # lazy plan; it is removed with the run's work directory.
    write_legacy_text(text, scratch, "dedup.csv")
    # Legacy mapper sessions ran with Spark 3.3 defaults: JVM in UTC and the
    # default ``timeParserPolicy`` (EXCEPTION) — under Ovid2's production
    # ``LEGACY`` policy CSV inference never yields timestamps.
    pinned = {"spark.sql.session.timeZone": "UTC", "spark.sql.legacy.timeParserPolicy": "EXCEPTION"}
    previous = {k: spark.conf.get(k, None) for k in pinned}
    for k, v in pinned.items():
        spark.conf.set(k, v)
    try:
        inferred = spark.read.load(
            scratch + "/dedup.csv",
            format="csv",
            sep="\t",
            inferSchema="true",
            header="true",
            quote='"',
            preferDate="false",
        )
        cols = []
        for i, field in enumerate(inferred.schema.fields):
            c = sf.col(f"`{field.name}`")
            if not isinstance(field.dataType, StringType):
                c = sf.when(c.isNull(), sf.lit(None).cast("string")).otherwise(
                    sf.to_csv(sf.struct(c), _MID_LINE_OPTIONS)
                )
            cols.append(c.alias(f"_c{i}"))
        inferred.select(*cols).write.mode("overwrite").parquet(scratch + "/inferred")
    finally:
        for k, v in previous.items():
            if v is None:
                spark.conf.unset(k)
            else:
                spark.conf.set(k, v)
    result: DataFrame = spark.read.parquet(scratch + "/inferred").toDF(*inferred.columns)
    return result
