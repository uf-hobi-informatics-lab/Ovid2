"""Spark-backed read/write for the disc driver.

The **only** module under :mod:`ovid2.drivers.disc` allowed to
import :mod:`pyspark`. Everything else in the disc package stays
web-safe.

CSV write strategy
------------------

The pre-2026-08 disc driver did ``coalesce(15).write.csv(...)``
followed by a pure-Python part-file merge. On a 25-worker cluster
that narrowed the last write stage to 15 executors and left the
rest idle — the exact bottleneck ``coalesce(1)`` produces, just
less extreme. Legacy Ovid avoided that with an
``awk``-based merge at *natural* partitioning.

We now do the same thing, plus two refinements: the header is
written exactly once (to ``_header.csv``) straight from the
DataFrame schema — no extra Spark job — and every part is written
with ``header=False``. The finalize step then concatenates
``[_header.csv, part-*.csv]`` *in parallel*: each input's offset is
a prefix sum of the file sizes, so the parts are copied concurrently
into a pre-sized output with ``os.copy_file_range``. The output is
byte-identical to a sequential ``cat``.

Parallelism is left to Spark by default. Set
``$OVID2_DISC_WRITE_PARTITIONS`` (a positive int) to reintroduce
``coalesce(N)`` on hosts whose local IOPS suffer under high
concurrent PUT.

Single-file behaviour is controlled by the address's
``location.single_file`` flag (default ``True`` for disc — matches
the pre-refactor contract). When ``False`` the driver writes the
Spark-native part directory verbatim.
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ovid2.config.settings import CSV_DELIMITER, DISC_WRITE_PARTITIONS

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

# Sample size for CSV delimiter auto-detection.
_DELIMITER_CANDIDATES: tuple[str, ...] = (",", "\t", "|", "~", ";")


def resolve_read_path(root_path: str, file_pattern: str) -> str:
    """Resolve a read path *exactly* under the address location.

    The address is the exact location of the data (see
    the adr-address-is-exact-location design note): the
    files live directly under ``root_path`` — no ``{pipeline}`` or
    ``{batch_id}`` segment is ever appended. ``file_pattern`` may be a
    plain name or a glob; a glob that matches only directories resolves
    to the first directory (Spark reads directories natively), files are
    preferred otherwise.

    Args:
        root_path: The address's exact directory (connection root plus
            ``location.path``).
        file_pattern: File name or glob (e.g. ``'*Demographic*'``).

    Returns:
        Resolved file or directory path.

    Raises:
        FileNotFoundError: If nothing under ``root_path`` matches.
    """
    full_pattern = os.path.join(root_path, file_pattern)
    if "*" in file_pattern or "?" in file_pattern:
        matches = sorted(glob.glob(full_pattern))
        file_matches = [m for m in matches if os.path.isfile(m)]
        if file_matches:
            return file_matches[0]
        if matches:
            return matches[0]
    elif os.path.exists(full_pattern):
        return full_pattern
    raise FileNotFoundError(f"No files found matching pattern '{file_pattern}' in {root_path}/")


def detect_delimiter(file_path: str) -> str:
    """Auto-detect the delimiter of a CSV file from its first line.

    When ``file_path`` is a directory, the first regular file inside is
    used as the sample. Returns ``','`` when nothing matches.
    """
    sample_path = file_path
    if os.path.isdir(file_path):
        children = sorted(
            f
            for f in os.listdir(file_path)
            if os.path.isfile(os.path.join(file_path, f))
            and not f.startswith(".")
            and not f.startswith("_")  # skip _SUCCESS / _metadata
        )
        if not children:
            return ","
        sample_path = os.path.join(file_path, children[0])

    with open(sample_path, encoding="utf-8", errors="replace") as fh:
        first_line = fh.readline()

    counts = {d: first_line.count(d) for d in _DELIMITER_CANDIDATES}  # noqa: OVID2001
    best = max(counts, key=counts.__getitem__)
    return best if counts[best] > 0 else ","


def read_dataframe(
    spark: SparkSession,
    root_path: str,
    file_name: str,
    delimiter: str | None,
    fmt: str,
    lowercase_columns: bool,
) -> DataFrame:
    """Read a DataFrame from disc.

    Extracted from :meth:`DiscAddress.read` so the driver can call
    this directly. All CSV-reader options that defended against the
    ACME 20260610 ``"externalmedid", "patid"`` whitespace-padded
    headers are preserved verbatim.
    """
    file_path = resolve_read_path(root_path, file_name)

    if fmt == "csv":
        detected_delimiter = delimiter or detect_delimiter(file_path)
        reader = (
            spark.read.option("header", "true")
            .option("inferSchema", "false")
            .option("delimiter", detected_delimiter)
            .option("quote", '"')
            .option("ignoreLeadingWhiteSpace", "true")
            .option("ignoreTrailingWhiteSpace", "true")
        )
        df = reader.csv(file_path)
    elif fmt == "parquet":
        df = spark.read.parquet(file_path)
    elif fmt == "json":
        df = spark.read.json(file_path)
    else:
        raise ValueError(f"Unsupported format: {fmt}")

    if lowercase_columns:
        df = df.toDF(*[c.lower() for c in df.columns])
    return df


def write_dataframe(
    df: DataFrame,
    root_path: str,
    table_name: str,
    delimiter: str | None,
    fmt: str,
    *,
    single_file: bool = True,
) -> None:
    """Write a DataFrame directly under the address directory ``root_path``.

    Args:
        df: Lazy Spark DataFrame to materialise.
        root_path: The address's exact output directory.
        table_name: File stem — the final artefact is
            ``{table_name}.{fmt}``.
        delimiter: CSV field separator. Defaults to
            :data:`ovid2.config.settings.CSV_DELIMITER` when ``None``.
        fmt: ``csv`` | ``parquet`` | ``json``.
        single_file: When ``True`` (default, matches the disc contract),
            the final CSV artefact is a single file. When ``False``,
            the Spark-native part-directory is left in place. Parquet
            and JSON writes always take the Spark-native shape —
            ``single_file`` is ignored for those formats.
    """
    output_dir = root_path
    output_file_name = f"{table_name}.{fmt}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        _write_csv(
            df,
            output_file_name,
            output_dir,
            delimiter or CSV_DELIMITER,
            single_file=single_file,
        )
    elif fmt == "parquet":
        file_path = os.path.join(output_dir, output_file_name)
        df.write.mode("overwrite").parquet(file_path)
    elif fmt == "json":
        file_path = os.path.join(output_dir, output_file_name)
        df.write.mode("overwrite").json(file_path)
    else:
        raise ValueError(f"Unsupported format: {fmt}")


def _cast_null_columns(df: DataFrame) -> DataFrame:
    """Cast NullType columns to string so CSV output is valid.

    Local imports keep this module importable without a Spark install
    (matches the pre-refactor pattern in ``core.addresses.disc``).
    """
    from pyspark.sql import functions as sf
    from pyspark.sql.types import NullType

    return df.select(
        *[
            sf.lit(None).cast("string").alias(field.name)
            if isinstance(field.dataType, NullType)
            else field.name
            for field in df.schema
        ]
    )


def _maybe_coalesce(df: DataFrame) -> DataFrame:
    """Apply the operator-configured coalesce, if any.

    Reads :data:`ovid2.config.settings.DISC_WRITE_PARTITIONS`. ``None``
    (the default) means "no coalesce" — Spark's natural partitioning
    drives the write and every executor participates. Positive int
    means "pin last stage to N", an escape hatch for local-IOPS-bound
    hosts.
    """
    if DISC_WRITE_PARTITIONS is None or DISC_WRITE_PARTITIONS <= 0:
        return df
    return df.coalesce(DISC_WRITE_PARTITIONS)


def _write_csv(
    df: DataFrame,
    output_file_name: str,
    output_dir: str,
    delimiter: str,
    *,
    single_file: bool,
) -> None:
    """Write CSV via Spark.

    When ``single_file`` is ``True`` (the disc default), the body
    parts are written *without* a header, the header is derived from
    the schema, and the finalize step concatenates
    ``[_header.csv, part-*.csv]`` in parallel. When ``False``, the writer
    emits the Spark-native part directory with headers on every part
    (mirrors what ``s3`` / ``azure_blob`` do by default).
    """
    output_file_path = os.path.join(output_dir, output_file_name)
    casted = _cast_null_columns(df)

    if not single_file:
        target = output_file_path
        (
            _maybe_coalesce(casted)
            .write.format("csv")
            .option("header", True)
            .option("nullValue", None)
            .option("delimiter", delimiter)
            .option("quote", "")
            .mode("overwrite")
            .save(target)
        )
        return

    work_directory = os.path.join(output_dir, f"tmp_{output_file_name}")

    # 1) Write the body parts with header disabled into work_directory —
    #    ``.save(...)`` in overwrite mode is destructive to any existing
    #    content at the target, so we must land the body **before** the
    #    header (otherwise the body write wipes the header).
    (
        _maybe_coalesce(casted)
        .write.format("csv")
        .option("header", False)
        .option("nullValue", None)
        .option("delimiter", delimiter)
        .option("quote", "")
        .mode("overwrite")
        .save(work_directory)
    )

    # 2) Write the header line (from the schema; no Spark job).
    _write_header(casted, work_directory, delimiter)

    # 3) Concat header + parts in parallel at precomputed offsets.
    _merge_csv_part_files(work_directory, output_file_path)
    shutil.rmtree(work_directory, ignore_errors=True)


def _header_line(columns: list[str], delimiter: str) -> bytes:
    """Return the CSV header line Spark would emit for ``columns``.

    The body is written with ``quote=""`` (Spark disables quoting), so
    the header is the plain delimiter-joined column list terminated by
    ``\n`` — exactly what the previous ``limit(0).coalesce(1)`` header
    job produced, without launching a Spark job.
    """

    # With ``quote=""`` Spark's univocity writer uses ``\x00`` as the quote
    # character and wraps any value containing the delimiter / a line
    # break in it. Mirror that so the header stays byte-identical.
    def _render(name: str) -> str:
        if delimiter in name or "\n" in name or "\r" in name:
            return f"\x00{name}\x00"
        return name

    return (delimiter.join(_render(c) for c in columns) + "\n").encode("utf-8")


def _write_header(df: DataFrame, work_directory: str, delimiter: str) -> None:
    """Emit exactly one header line into ``{work_directory}/_header.csv``.

    Derived from the (already analysed) DataFrame schema on the driver —
    no Spark job. Idempotent: overwrites any prior header file.
    """
    Path(work_directory).mkdir(parents=True, exist_ok=True)
    header_dst = os.path.join(work_directory, "_header.csv")
    with open(header_dst, "wb") as fh:
        fh.write(_header_line(list(df.columns), delimiter))


#: Parallel merge worker cap. The merge is I/O bound; a handful of
#: concurrent ``copy_file_range`` streams saturates local / NFS disks.
_MERGE_MAX_WORKERS: int = 16


def _copy_range(src: str, dst_fd: int, offset: int, size: int) -> None:
    """Copy ``size`` bytes of ``src`` into ``dst_fd`` at ``offset``.

    Uses :func:`os.copy_file_range` (in-kernel, zero-copy on Linux) and
    falls back to ``pread``/``pwrite`` chunks where it is unsupported
    (cross-filesystem on old kernels, non-Linux hosts).
    """
    with open(src, "rb") as in_fh:
        in_fd = in_fh.fileno()
        copied = 0
        copy_range = getattr(os, "copy_file_range", None)
        if copy_range is not None:
            try:
                while copied < size:
                    n = copy_range(in_fd, dst_fd, size - copied, copied, offset + copied)
                    if n == 0:
                        break
                    copied += n
            except OSError:
                copied = 0
        chunk = 8 * 1024 * 1024
        while copied < size:
            data = os.pread(in_fd, min(chunk, size - copied), copied)
            if not data:
                break
            os.pwrite(dst_fd, data, offset + copied)
            copied += len(data)
    if copied != size:
        raise OSError(f"Short copy merging {src}: {copied}/{size} bytes")


def _merge_csv_part_files(work_directory: str, output_file_path: str) -> None:
    """Concatenate ``_header.csv`` + ``part-*.csv`` into one file.

    Each input's byte offset in the output is known up front (prefix sum
    of file sizes), so the parts are copied **in parallel** into a
    pre-sized output file with :func:`os.copy_file_range`. The result is
    byte-identical to a sequential ``cat header part-00000 part-00001 …``
    (inputs are sorted the same way), but no longer serialises the whole
    table through one stream.

    Raises:
        FileNotFoundError: If the header or every ``part-*.csv`` is missing.
    """
    from concurrent.futures import ThreadPoolExecutor

    header = os.path.join(work_directory, "_header.csv")
    if not os.path.isfile(header):
        raise FileNotFoundError(
            f"Missing header stage under {work_directory} — the single-file merge cannot start."
        )
    part_files = sorted(glob.glob(os.path.join(work_directory, "part-*.csv")))
    if not part_files:
        raise FileNotFoundError(f"No part files produced by Spark in {work_directory}.")

    inputs = [header, *part_files]
    sizes = [os.path.getsize(p) for p in inputs]
    offsets: list[int] = []
    running = 0
    for size in sizes:
        offsets.append(running)
        running += size

    fd = os.open(output_file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, running)
        jobs = [(p, o, s) for p, o, s in zip(inputs, offsets, sizes, strict=True) if s > 0]
        workers = max(1, min(_MERGE_MAX_WORKERS, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_copy_range, p, fd, o, s) for p, o, s in jobs]
            for future in futures:
                future.result()
    finally:
        os.close(fd)


__all__ = [
    "detect_delimiter",
    "read_dataframe",
    "resolve_read_path",
    "write_dataframe",
]
