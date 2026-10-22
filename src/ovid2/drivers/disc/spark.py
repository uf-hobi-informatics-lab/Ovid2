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

We now do the same thing, plus a small refinement: rather than
skipping the header of every part in the merge step, we write the
header exactly once (to ``_header.csv``) and write every part
with ``header=False``. The finalize step is then a plain ``cat``
of ``[_header.csv, part-*.csv]`` — no per-line predicate, single
sequential I/O pass on local disk.

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
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ovid2.config.settings import CSV_DELIMITER, DISC_WRITE_PARTITIONS

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

# Sample size for CSV delimiter auto-detection.
_DELIMITER_CANDIDATES: tuple[str, ...] = (",", "\t", "|", "~", ";")


def resolve_read_path(
    root_path: str,
    pipeline: str,
    batch_id: str,
    file_pattern: str,
) -> str:
    """Resolve a pipeline-scoped read path (case-insensitive, glob-aware).

    Primary structure: ``{root_path}/{pipeline}/{batch_id}/{file_pattern}``,
    tried case-insensitively over the pipeline segment (``pipeline``,
    ``pipeline.lower()``, ``pipeline.upper()``).

    Flat fallback: when *none* of the pipeline-scoped layouts match,
    the pipeline segment is dropped and ``{root_path}/{batch_id}/
    {file_pattern}`` is tried. This supports drops where the disc
    address root already points at the partner directory and the batch
    dir sits directly beneath it (e.g. ``inbox_demo__root`` →
    ``/data/inbox/DEMO/20260610/`` for pipeline ``demo``).
    The fallback is a strict superset of the primary resolution: the
    pipelined layout always wins when it exists, so no existing pipeline
    is affected.

    Root-direct fallback: when neither the pipelined nor the flat
    layout matches, ``{root_path}/{file_pattern}`` is tried last. This
    catches the common operator misconfiguration where the batch id is
    *baked into the address root* (``location.path: "20260610"`` on top
    of a connection whose ``host_path`` is the partner dir), so
    ``root_path`` is already ``/data/inboxarchive/ACME/20260610`` and the
    files sit directly beneath it. Without this, such an address
    double-applies the batch id (``…/20260610/demo/20260610/``) and every
    table fails with a confusing "file not found". Tried last, so it can
    never shadow a correctly-scoped layout.

    Falls back to a directory match when the glob resolves to a folder
    (Spark reads directories natively).

    Args:
        root_path: The disc address's root directory.
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        file_pattern: File name or glob (e.g. ``'*Demographic*'``).

    Returns:
        Resolved file or directory path.

    Raises:
        FileNotFoundError: If no path matches under either layout.
    """
    pipeline_variations = (pipeline, pipeline.lower(), pipeline.upper())
    first_directory_match: str | None = None

    # De-duplicate the base paths while preserving order: the pipelined
    # layouts first (primary), then the flat ``{root}/{batch_id}`` layout
    # (fallback). ``os.path.join(root, "", batch_id)`` already collapses an
    # empty pipeline segment, so the synthetic empty-pipeline caller in
    # ``core.pipeline`` degenerates to the flat layout without a second pass.
    base_paths: list[str] = []
    for pipeline_name in pipeline_variations:
        base_path = os.path.join(root_path, pipeline_name, batch_id)
        if base_path not in base_paths:
            base_paths.append(base_path)
    flat_base = os.path.join(root_path, batch_id)
    if flat_base not in base_paths:
        base_paths.append(flat_base)
    # Root-direct layout (tried last): the batch id is already baked into
    # ``root_path`` and the files sit directly beneath it. Catches the
    # ``location.path: "<batch_id>"`` misconfiguration without shadowing
    # any correctly-scoped layout above.
    if root_path not in base_paths:
        base_paths.append(root_path)

    for base_path in base_paths:
        full_pattern = os.path.join(base_path, file_pattern)

        if "*" in file_pattern or "?" in file_pattern:
            matches = glob.glob(full_pattern)
            if matches:
                file_matches = [m for m in matches if os.path.isfile(m)]
                if file_matches:
                    return file_matches[0]
                if first_directory_match is None:
                    first_directory_match = matches[0]
        elif os.path.exists(full_pattern):
            return full_pattern

    if first_directory_match is not None:
        return first_directory_match

    raise FileNotFoundError(
        f"No files found matching pattern '{file_pattern}' in "
        f"{root_path}/[{pipeline}|{pipeline.upper()}]/{batch_id}/, "
        f"the flat layout {root_path}/{batch_id}/, "
        f"or the root-direct layout {root_path}/"
    )


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
    pipeline: str,
    batch_id: str,
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
    file_path = resolve_read_path(root_path, pipeline, batch_id, file_name)

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
    pipeline: str,
    batch_id: str,
    table_name: str,
    delimiter: str | None,
    fmt: str,
    *,
    single_file: bool = True,
) -> None:
    """Write a DataFrame under ``{root_path}/{pipeline}/{batch_id}/``.

    Args:
        df: Lazy Spark DataFrame to materialise.
        root_path: Address root.
        pipeline: Pipeline name (sub-directory).
        batch_id: Batch id (sub-directory).
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
    output_dir = os.path.join(root_path, pipeline, batch_id)
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
    parts are written *without* a header and the header is captured
    once via ``limit(0)``; the finalize step is then a plain ``cat``
    of ``[_header.csv, part-*.csv]``. When ``False``, the writer
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

    # 2) Materialise the header exactly once into a sibling staging dir,
    #    then move it into work_directory so the merge step sees the
    #    fixed ``_header.csv`` filename.
    _write_header(casted, work_directory, delimiter)

    # 3) Concat header + parts server-side (local disk here). ``cat``
    #    is the fastest single-pass merger available; the previous
    #    Python ``shutil.copyfileobj`` merger was single-threaded and
    #    also had to strip a header from every subsequent part.
    _merge_csv_part_files(work_directory, output_file_path)
    shutil.rmtree(work_directory, ignore_errors=True)


def _write_header(df: DataFrame, work_directory: str, delimiter: str) -> None:
    """Emit exactly one header line into ``{work_directory}/_header.csv``.

    Uses ``limit(0)`` so no body rows are materialised. The Spark
    writer still produces a directory with a single ``part-*.csv``
    file containing only the header row — we rename that to the
    known ``_header.csv`` path so the finalize step is
    order-deterministic.

    Idempotent: overwrites any prior ``_header_stage`` directory.
    """
    header_stage = os.path.join(work_directory, "_header_stage")
    (
        df.limit(0)
        .coalesce(1)
        .write.format("csv")
        .option("header", True)
        .option("nullValue", None)
        .option("delimiter", delimiter)
        .option("quote", "")
        .mode("overwrite")
        .save(header_stage)
    )
    parts = sorted(glob.glob(os.path.join(header_stage, "part-*.csv")))
    if not parts:
        raise FileNotFoundError(f"Spark produced no header part-file under {header_stage}.")
    header_dst = os.path.join(work_directory, "_header.csv")
    # Move the first part; there is only one because of coalesce(1)/limit(0).
    Path(work_directory).mkdir(parents=True, exist_ok=True)
    shutil.move(parts[0], header_dst)
    shutil.rmtree(header_stage, ignore_errors=True)


def _merge_csv_part_files(work_directory: str, output_file_path: str) -> None:
    """Concatenate ``_header.csv`` + ``part-*.csv`` into one file.

    Uses ``cat`` via :func:`subprocess.run` with ``shell=False`` and
    an explicit sorted file list — no glob expansion in the shell,
    no untrusted arguments. Falls back to a pure-Python concat when
    ``cat`` is absent (e.g. Windows dev laptops); the fallback is
    slower but semantically identical.

    Raises:
        FileNotFoundError: If Spark produced no ``part-*.csv`` files.
    """
    header = os.path.join(work_directory, "_header.csv")
    if not os.path.isfile(header):
        raise FileNotFoundError(
            f"Missing header stage under {work_directory} — the single-file merge cannot start."
        )
    part_files = sorted(glob.glob(os.path.join(work_directory, "part-*.csv")))
    if not part_files:
        raise FileNotFoundError(f"No part files produced by Spark in {work_directory}.")

    inputs = [header, *part_files]
    cat = shutil.which("cat")
    if cat is not None:
        with open(output_file_path, "wb") as out_fh:
            subprocess.run(
                [cat, *inputs],
                check=True,
                stdout=out_fh,
            )
        return

    # Pure-Python fallback (portable, slower).
    with open(output_file_path, "wb") as out_fh:
        for path in inputs:
            with open(path, "rb") as in_fh:
                shutil.copyfileobj(in_fh, out_fh)


__all__ = [
    "detect_delimiter",
    "read_dataframe",
    "resolve_read_path",
    "write_dataframe",
]
