"""Engine-portable legacy CSV text I/O (``legacy_ovid`` stage boundaries).

Legacy Ovid wrote a tab CSV after each stage and read it back with
Spark (:mod:`ovid2.legacy.csv_roundtrip`). On Spark the files are written
and read with Spark itself. On every other engine the *same bytes* are
produced and parsed with :mod:`ovid2.core.engines.csv_text` (the
univocity model pinned to Spark), and frames move in and out of the
engine as Arrow:

* :func:`write_frame_file` — legacy ``write_pyspark_output_file`` (Spark
  CSV writer rules, header from the schema, ``NullType`` → string).
* :func:`write_text_file` — a frame whose cells already *are* file text
  (the dedup stage output), written verbatim.
* :func:`read_tab_file` — legacy ``spark_read`` / ``spark.read.load`` of a
  tab file (``header``, ``quote='"'``, ``escape`` per dialect,
  ``multiLine=false``, ``inferSchema=false``): every column a string,
  Spark's safe header names, blank lines dropped.

Rows are streamed in batches, so memory stays bounded. Every helper takes
the engine's session / frame and never imports an engine library.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from ovid2.core.engines.csv_text import is_blank_line, parse_fields, parse_line, write_line

__all__ = ["read_lines", "read_tab_file", "write_frame_file", "write_text_file"]

_BATCH = 65_536


def _safe_header(columns: list[str]) -> list[str]:
    from ovid2.legacy.csv_roundtrip import safe_header

    return safe_header(columns)


def _iter_records(path: str) -> Iterator[str]:
    """Yield the records of ``path`` split like Spark's line reader (``\\n`` / ``\\r\\n`` / ``\\r``)."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        pending = ""
        while True:
            chunk = fh.read(8 * 1024 * 1024)
            if not chunk:
                break
            text = pending + chunk
            # A trailing CR may be the first half of CRLF: keep it for the next chunk.
            cut = len(text) - 1 if text.endswith("\r") else len(text)
            start = 0
            i = 0
            while i < cut:
                ch = text[i]
                if ch == "\n" or ch == "\r":
                    yield text[start:i]
                    if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                        i += 1
                    start = i + 1
                i += 1
            pending = text[start:]
        if pending:
            if pending.endswith("\r"):
                pending = pending[:-1]
                yield pending
            else:
                yield pending


def _files(path: str) -> list[str]:
    import glob as _glob

    if os.path.isdir(path):
        return sorted(
            os.path.join(path, f)
            for f in os.listdir(path)
            if os.path.isfile(os.path.join(path, f)) and not f.startswith((".", "_"))
        )
    if any(c in path for c in "*?["):
        return sorted(m for m in _glob.glob(path) if os.path.isfile(m))
    return [path]


def read_tab_file(
    session: Any,
    path: str,
    *,
    escape: str = '"',
    delimiter: str = "\t",
    quote: str = '"',
    header: bool = True,
) -> Any:
    """Read a CSV file exactly like Spark's CSV reader (``multiLine=false``), as an engine frame.

    Args:
        session: The engine session (``from_arrow`` / ``createDataFrame``).
        path: File, directory or glob (Spark reads every matching file;
            with ``header`` each file's first record is skipped, names
            come from the first file).
        escape: Quote-escape char (``'"'`` for ``cf.spark_read``,
            ``'\\'`` for Spark's default).
        delimiter: Field delimiter.
        quote: Quote char.
        header: First record is the header.

    Returns:
        A lazy frame with all-string columns (Spark's safe header names, or
        ``_c0 .. _cN`` without a header).

    Raises:
        FileNotFoundError: When nothing matches ``path`` (Spark's
            ``PATH_NOT_FOUND``).
    """
    files = _files(path)
    if not files:
        raise FileNotFoundError(f"[PATH_NOT_FOUND] Path does not exist: {path}")
    native = _native_engine(session)
    if native is not None and len(delimiter) == 1 and len(quote) == 1 and len(escape) == 1:
        # Same univocity split, but lazy and parallel (SQL fast path +
        # exact Python parser only for irregular records).
        from ovid2.core.engines.duckdb_stager import read_spark_default_csv

        return read_spark_default_csv(
            native, files, delimiter=delimiter, quote=quote, escape=escape, header=header
        )
    first_record: list[str | None] | None = None
    for f in files:
        for rec in _iter_records(f):
            if not is_blank_line(rec):
                first_record = parse_fields(rec, escape=escape, delimiter=delimiter, quote=quote)
                break
        if first_record is not None:
            break
    if header:
        names = _safe_header([v or "" for v in (first_record or ["_c0"])])
    else:
        names = [f"_c{i}" for i in range(max(1, len(first_record or [None])))]
    width = len(names)

    def _rows() -> Iterator[list[str | None]]:
        for f in files:
            skip = header
            for rec in _iter_records(f):
                if is_blank_line(rec):
                    continue
                if skip:
                    skip = False
                    continue
                yield parse_line(rec, width=width, escape=escape, delimiter=delimiter, quote=quote)

    return _frame_from_rows(session, names, _rows())


def read_lines(session: Any, path: str) -> Any:
    """Spark ``read.text``: one ``value`` column per line."""
    files = _files(path)
    if not files:
        raise FileNotFoundError(f"[PATH_NOT_FOUND] Path does not exist: {path}")

    def _rows() -> Iterator[list[str | None]]:
        for f in files:
            for rec in _iter_records(f):
                yield [rec]

    return _frame_from_rows(session, ["value"], _rows())


def _frame_from_rows(session: Any, names: list[str], rows: Iterator[list[str | None]]) -> Any:
    """Build an all-string engine frame from a row stream (Arrow batches)."""
    import pyarrow as pa

    positional = [f"_p{i}" for i in range(len(names))]
    schema = pa.schema([(n, pa.string()) for n in positional])
    batches: list[Any] = []
    buf: list[list[str | None]] = []
    for row in rows:
        buf.append(row)
        if len(buf) >= _BATCH:
            batches.append(
                pa.RecordBatch.from_arrays(
                    list(map(pa.array, zip(*buf, strict=True))), schema=schema
                )
            )
            buf = []
    if buf:
        cols = [pa.array(list(c), pa.string()) for c in zip(*buf, strict=True)]
        batches.append(pa.RecordBatch.from_arrays(cols, schema=schema))
    table = pa.Table.from_batches(batches, schema=schema)
    frame = _from_arrow(session, table)
    return frame.toDF(*names)


def _from_arrow(session: Any, table: Any) -> Any:
    engine_from_arrow = getattr(session, "from_arrow", None)
    if callable(engine_from_arrow):
        return engine_from_arrow(table)
    return session.createDataFrame(table.to_pandas())


def _arrow_batches(df: Any) -> Iterator[Any]:
    to_batches = getattr(df, "to_arrow_batches", None)
    if callable(to_batches):
        yield from to_batches()
        return
    yield from df.to_arrow().to_batches()


def write_frame_file(df: Any, folder: str, output_file_name: str) -> None:
    """Legacy ``write_pyspark_output_file`` on any engine (Spark CSV writer bytes)."""
    _write(df, folder, output_file_name, raw=False)


def write_text_file(df: Any, folder: str, output_file_name: str, *, header: bool = True) -> None:
    """Write a frame whose cells already are legacy file text, verbatim."""
    _write(df, folder, output_file_name, raw=True, header=header)


def _native_engine(obj: Any) -> Any:
    """The DuckDB engine behind a DuckDB session / frame, else ``None``.

    Duck-typed (no engine import): the DuckDB session and frame expose
    ``_engine`` with ``type_name == "duckdb"``.
    """
    engine = getattr(obj, "_engine", None)
    if engine is None or getattr(engine, "type_name", None) != "duckdb":
        return None
    from ovid2.core.engines.duckdb_stager import native_text_enabled

    return engine if native_text_enabled() else None


def _write(df: Any, folder: str, output_file_name: str, *, raw: bool, header: bool = True) -> None:
    from ovid2.core.engines._duck_udf import python_value_text

    os.makedirs(folder, exist_ok=True)
    final_path = os.path.join(folder, output_file_name).replace("tmp_", "")
    engine = _native_engine(df)
    if engine is not None and getattr(df, "relation", None) is not None:
        from ovid2.core.engines import dsl
        from ovid2.core.engines.duckdb_stager import write_legacy_text
        from ovid2.core.engines.types import StringType

        if write_legacy_text(
            engine,
            df,
            final_path,
            raw=raw,
            header=header,
            python_text=dsl.udf(python_value_text, StringType()),
        ):
            return
    partial = final_path + ".partial"
    columns = list(df.columns)
    try:
        with open(partial, "w", encoding="utf-8", newline="") as out:
            if header:
                out.write("\t".join(columns) + "\n")
            for batch in _arrow_batches(df):
                cols = [c.to_pylist() for c in batch.columns]
                for row in zip(*cols, strict=True):
                    if raw:
                        out.write("\t".join("" if v is None else str(v) for v in row) + "\n")
                    else:
                        out.write(write_line([python_value_text(v) for v in row]) + "\n")
        os.replace(partial, final_path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
