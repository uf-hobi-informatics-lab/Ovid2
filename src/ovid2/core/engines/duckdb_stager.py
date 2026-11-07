"""DuckDB stager: moves data between drivers and a :class:`DuckDBEngine`.

One :class:`DuckDBStagingStrategy` per driver type lives in
``src/ovid2/drivers/<type>/duckdb.py`` and registers itself with
:func:`register_duckdb_strategy`. The stager dispatches on
``driver.type_name``.

Shared helpers here implement the **disc CSV contract** every strategy
reuses (S3 / Azure / GCS read and write the same text format):

* :func:`read_csv_relation` — Spark ``header=true, inferSchema=false,
  quote='"', ignoreLeading/TrailingWhiteSpace=true``: every column is
  VARCHAR, values are space-trimmed, empty -> NULL, ragged rows are
  NULL-padded / truncated, invalid UTF-8 is replaced, and duplicate /
  empty header names are renamed exactly like Spark (``a0``/``a1``,
  ``_c2``).
* :func:`write_csv_file` — Spark's disc output (``quote=""``): no
  quoting except values containing the delimiter / a line break, which
  are wrapped in ``\\x00`` (univocity's "no quote" character); NULL ->
  empty; doubles rendered Java-style (``1.0E20``); timestamps
  ``yyyy-MM-ddTHH:mm:ss.SSSZ``-free plain text.

ovid2: allow-sql-fstring — SQL here interpolates only quoted
identifiers, parameter placeholders, and escaped literals produced by
:func:`~._java_datetime.sql_string`.
"""

from __future__ import annotations

import contextlib
import glob as _glob
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ovid2.core.engines._duck_column import ident
from ovid2.core.engines._java_datetime import sql_string
from ovid2.core.engines.stager import EngineStager, EngineStagerUnsupported

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.core.engines.duckdb import DuckDBEngine
    from ovid2.drivers.base import Driver

_log = logging.getLogger(__name__)

#: Guards creation of the per-source scanner-probe locks.
_SCANNER_LOCKS_GUARD = threading.Lock()

__all__ = [
    "DuckDBEngineStager",
    "DuckDBStagingStrategy",
    "csv_select_list",
    "parquet_glob",
    "read_csv_relation",
    "register_duckdb_strategy",
    "spark_header_names",
    "write_csv_file",
    "write_frame_local",
]


# ---------------------------------------------------------------------------
# Strategy ABC + stager
# ---------------------------------------------------------------------------


class DuckDBStagingStrategy(ABC):
    """Per-driver-type data plane for the DuckDB engine."""

    #: Driver type serviced (key of ``ovid2.drivers.registry.DRIVERS``).
    driver_type: ClassVar[str] = ""

    def __init__(self, engine: DuckDBEngine) -> None:
        self._engine = engine

    @abstractmethod
    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> DuckFrame:
        """Return a lazy :class:`DuckFrame` over the driver's data."""

    @abstractmethod
    def persist(
        self,
        df: DuckFrame,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Execute ``df`` and write it to the driver's exact location."""


class DuckDBEngineStager(EngineStager):
    """Stager for the ``duckdb`` engine family (dispatch on driver type)."""

    family = "duckdb"

    #: driver type -> strategy class (filled by :func:`register_duckdb_strategy`).
    _STRATEGIES: ClassVar[dict[str, type[DuckDBStagingStrategy]]] = {}

    #: Modules imported lazily on first use so their registration runs.
    _STRATEGY_MODULES: ClassVar[dict[str, str]] = {
        "disc": "ovid2.drivers.disc.duckdb",
        "s3": "ovid2.drivers.s3.duckdb",
        "azure_blob": "ovid2.drivers.azure_blob.duckdb",
        "gcs": "ovid2.drivers.gcs.duckdb",
        "snowflake": "ovid2.drivers.snowflake.duckdb",
        "postgres": "ovid2.drivers.postgres.duckdb",
        "mssql": "ovid2.drivers.mssql.duckdb",
        "access": "ovid2.drivers.access.duckdb",
        "databricks": "ovid2.drivers.databricks.duckdb",
    }

    @classmethod
    def supports(cls, driver_type: str) -> bool:
        """Return ``True`` when a strategy module exists for ``driver_type``."""
        return driver_type in cls._STRATEGY_MODULES

    @classmethod
    def supported_driver_types(cls) -> frozenset[str]:
        """Every driver type with a DuckDB strategy."""
        return frozenset(cls._STRATEGY_MODULES)

    def _strategy(self, driver: Driver) -> DuckDBStagingStrategy:
        driver_type = str(getattr(driver, "type_name", "") or "")
        strategy_cls = self._STRATEGIES.get(driver_type)
        if strategy_cls is None and driver_type in self._STRATEGY_MODULES:
            import importlib

            importlib.import_module(self._STRATEGY_MODULES[driver_type])
            strategy_cls = self._STRATEGIES.get(driver_type)
        if strategy_cls is None:
            raise EngineStagerUnsupported(self.family, driver_type)
        return strategy_cls(self._engine)  # type: ignore[arg-type]

    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        """Dispatch to the driver type's strategy."""
        return self._strategy(driver).materialise(
            driver, pipeline, batch_id, file_name, delimiter, table_name=table_name
        )

    def persist(
        self,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Dispatch to the driver type's strategy."""
        engine: Any = self._engine
        engine.current_stage = f"write {table_name}"
        try:
            self._strategy(driver).persist(df, driver, pipeline, batch_id, table_name, delimiter)
        finally:
            engine.current_stage = None


def register_duckdb_strategy(
    strategy_cls: type[DuckDBStagingStrategy],
) -> type[DuckDBStagingStrategy]:
    """Register ``strategy_cls`` for its ``driver_type`` (usable as a decorator)."""
    if not strategy_cls.driver_type:
        raise ValueError(f"DuckDB strategy {strategy_cls!r} declares no driver_type")
    DuckDBEngineStager._STRATEGIES[strategy_cls.driver_type] = strategy_cls
    return strategy_cls


# ---------------------------------------------------------------------------
# CSV read (Spark disc reader contract)
# ---------------------------------------------------------------------------


def spark_header_names(raw: list[str | None]) -> list[str]:
    """Rename a CSV header exactly like Spark's CSV reader.

    Values are space-trimmed (``ignoreLeading/TrailingWhiteSpace``);
    empty / NULL names become ``_c<i>``; names that repeat
    (case-insensitively, Spark's default) get their position appended.
    """
    trimmed = [("" if v is None else str(v).strip(" ")) for v in raw]
    lowered = [t.lower() for t in trimmed]
    out: list[str] = []
    for i, name in enumerate(trimmed):
        if name == "":
            out.append(f"_c{i}")
        elif sum(1 for other in lowered if other == name.lower()) > 1:
            out.append(f"{name}{i}")
        else:
            out.append(name)
    return out


def _sniff_header(files: list[str], delimiter: str) -> list[str]:
    """Return the raw header fields of the first file (Spark uses one)."""
    # Header parsing must use the real delimiter + quote rules: read the
    # first record as a ragged row of up to N columns.
    with open(files[0], "rb") as fh:
        first = fh.readline().decode("utf-8", errors="replace").rstrip("\r\n")
    if first.startswith("\ufeff"):
        first = first[1:]
    return _split_csv_line(first, delimiter)


def _split_csv_line(line: str, delimiter: str) -> list[str]:
    """Split one CSV record honouring ``"`` quoting (header use only).

    Mirrors Spark's ``ignoreLeadingWhiteSpace``: a quoted field preceded
    by spaces (``"a", "b"``) is still unquoted, so the names carry no
    stray ``"`` characters.
    """
    import csv
    import io

    reader = csv.reader(
        io.StringIO(line),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        skipinitialspace=delimiter != " ",
    )
    try:
        fields = next(reader)
    except StopIteration:
        return []
    out: list[str] = []
    for field in fields:
        stripped = field.strip(" ")
        # ``"name" `` (trailing space after the closing quote) leaves the
        # quotes in place; strip one balanced pair like Spark does.
        if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
            stripped = stripped[1:-1].replace('""', '"')
        out.append(stripped)
    return out


def _resolve_files(path: Any) -> list[str]:
    """Expand a file / directory / glob / list into concrete data files."""
    paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    out: list[str] = []
    for p in paths:
        p = str(p)
        if os.path.isdir(p):
            children = sorted(
                os.path.join(p, f)
                for f in os.listdir(p)
                if os.path.isfile(os.path.join(p, f)) and not f.startswith((".", "_"))
            )
            out.extend(children)
        elif any(ch in p for ch in "*?["):
            out.extend(sorted(m for m in _glob.glob(p) if os.path.isfile(m)))
        else:
            out.append(p)
    return out


def csv_select_list(width: int, names: list[str], *, trim: bool = True) -> str:
    """Return the projection mapping raw ``c<i>`` columns to Spark names."""
    items = []
    for i, name in enumerate(names):
        src = ident(f"c{i}")
        expr = f"NULLIF(trim({src}), '')" if trim else src
        items.append(f"{expr} AS {ident(name)}")
    del width
    return ", ".join(items)


def read_csv_relation(
    conn: Any,
    path: Any,
    *,
    delimiter: str = ",",
    header: bool = True,
    names: list[str] | None = None,
    lowercase_columns: bool = False,
    with_filename: bool = False,
    parallel: bool = True,
    strict: bool = False,
) -> Any:
    """Return a lazy relation reading CSV text with Spark disc-reader semantics.

    Args:
        conn: DuckDB connection.
        path: File, directory (Spark part dir), glob, or list of those.
            Remote URLs (``s3://``, ``az://``, ``gs://``) are passed through.
        delimiter: Field delimiter.
        header: Whether the first line of each file is a header.
        names: Override the header (remote reads pass the sniffed names).
        lowercase_columns: Lower-case the final column names.
        with_filename: Append ``__ovid2_filename`` (``input_file_name``).
        parallel: Use DuckDB's parallel CSV scanner. It refuses some valid
            files (``NotImplementedException: The Parallel CSV Reader …``);
            :func:`read_csv_frame` retries those with ``parallel=False``.
        strict: Parse in DuckDB's ``strict_mode`` without NULL padding.
            The parallel scanner accepts quoted line breaks only in this
            mode; any ragged / malformed row raises instead of being
            padded, so a successful strict read is row-identical to the
            lenient one. :func:`read_csv_frame` tries it first and falls
            back to the lenient (Spark-parity) single-threaded parse.
    """
    remote = isinstance(path, str) and re.match(r"^[a-z][a-z0-9+]*://", path) is not None
    files = [path] if remote else _resolve_files(path)
    if not files:
        raise FileNotFoundError(f"No CSV files found at {path!r}")
    if names is None:
        raw: list[str | None] = list(_sniff_header(files, delimiter))
        # Spark (header=false) names columns ``_c0.._cN`` from the width
        # of the first record.
        names = spark_header_names(raw) if header else [f"_c{i}" for i in range(max(1, len(raw)))]
    if lowercase_columns:
        names = [n.lower() for n in names]
    width = len(names)
    columns = "{" + ", ".join(f"{sql_string(f'c{i}')}: 'VARCHAR'" for i in range(width)) + "}"
    leniency = (
        "strict_mode = true, null_padding = false"
        if strict
        else "strict_mode = false, null_padding = true"
    )
    options = (
        f"header = false, skip = {1 if header else 0}, delim = {sql_string(delimiter)}, "
        "quote = '\"', escape = '\"', auto_detect = false, "
        f"columns = {columns}, {leniency}, "
        f"ignore_errors = false, max_line_size = {_max_line_size()}, "
        f"encoding = 'utf-8', parallel = {'true' if parallel else 'false'}"
    )
    if with_filename:
        options += ", filename = true"
    file_list = "[" + ", ".join(sql_string(str(f)) for f in files) + "]"
    select = csv_select_list(width, names)
    if with_filename:
        select += ", filename AS __ovid2_filename"
    rel = conn.sql(f"SELECT {select} FROM read_csv({file_list}, {options})")
    return rel


def _max_line_size() -> int:
    """Longest CSV record accepted (bytes); ``$OVID2_DUCKDB_MAX_LINE_SIZE``.

    DuckDB sizes its per-thread read buffers from this, so the default
    stays modest (8 MiB, 4x DuckDB's own) — raise it only for sources
    with very long free-text records.
    """
    raw = os.environ.get("OVID2_DUCKDB_MAX_LINE_SIZE", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 8 * 1024 * 1024


def _scanner_probe_lock(engine: Any, key: str) -> Any:
    """Return the per-source lock serialising the CSV scanner probe.

    Locks live on the engine (``engine._csv_scanner_locks``) so they span
    every parallel table of the run; the dict itself is guarded by a
    module-level lock.
    """
    with _SCANNER_LOCKS_GUARD:
        locks: dict[str, Any] = engine.__dict__.setdefault("_csv_scanner_locks", {})
        lock = locks.get(key)
        if lock is None:
            lock = threading.Lock()
            locks[key] = lock
        return lock


def read_csv_frame(
    engine: Any,
    path: Any,
    *,
    delimiter: str,
    lowercase_columns: bool = False,
    name: str = "source",
) -> Any:
    """Read CSV text into a staged :class:`DuckFrame` (Spark disc contract).

    The parse is staged once into scratch Parquet
    (:meth:`DuckDBEngine.stage_source`). DuckDB rejects invalid UTF-8
    outright, whereas Spark's reader replaces bad bytes with U+FFFD; on
    that error the offending files are transcoded into scratch with the
    same replacement and re-read, so no row is lost.

    Scanner ladder (the first that succeeds wins):

    1. **parallel strict** — DuckDB's parallel scanner refuses quoted
       line breaks combined with ``null_padding`` (ACME's 4.4 GB
       ``visit_occurrence.csv``: 33 s serial vs 3 s parallel), but
       accepts them in ``strict_mode``. Strict mode raises on any row a
       lenient parse would pad / repair, so success is row-identical.
    2. **parallel lenient** — the historical default.
    3. **serial lenient** — the parallel scanner refused the file.

    The rung that worked is remembered per file set on the engine
    (``engine._csv_scanner_modes``), so a source read by several tables
    never re-pays the failed attempts.
    """
    conn = engine.connection()
    modes: dict[str, tuple[bool, bool]] = engine.__dict__.setdefault("_csv_scanner_modes", {})

    def _frame(source: Any, *, parallel: bool, strict: bool) -> Any:
        rel = read_csv_relation(
            conn,
            source,
            delimiter=delimiter,
            header=True,
            lowercase_columns=lowercase_columns,
            parallel=parallel,
            strict=strict,
        )
        return engine.stage_source(engine.wrap(rel), name=name)

    def _frame_any_scanner(source: Any) -> Any:
        key = repr(source)
        known = modes.get(key)
        if known is not None:
            return _frame(source, parallel=known[0], strict=known[1])
        # Parallel tables often read the same source file: serialise the
        # scanner probe per file so the first reader records the rung
        # that worked and the others reuse it, instead of each paying
        # the failed attempts (and a single-threaded re-parse) again.
        with _scanner_probe_lock(engine, key):
            known = modes.get(key)
            if known is not None:
                return _frame(source, parallel=known[0], strict=known[1])
            return _probe_scanner(source, key)

    def _probe_scanner(source: Any, key: str) -> Any:
        # Strict mode is only safe when the parse runs *now* (staged): a
        # lazy strict read would raise at write time, past the fallback.
        if engine.stage_sources_enabled():
            try:
                frame = _frame(source, parallel=True, strict=True)
                modes[key] = (True, True)
                return frame
            except Exception as exc:
                if not _is_strict_rejection(exc):
                    raise
        try:
            frame = _frame(source, parallel=True, strict=False)
            modes[key] = (True, False)
            return frame
        except Exception as exc:
            if not _is_parallel_scanner_refusal(exc):
                raise
            _log.warning(
                "DuckDB's parallel CSV reader refused %s; re-reading single-threaded.", source
            )
        frame = _frame(source, parallel=False, strict=False)
        modes[key] = (False, False)
        return frame

    try:
        return _frame_any_scanner(path)
    except Exception as exc:
        if "unicode" not in str(exc).lower() and "utf-8" not in str(exc).lower():
            raise
        _log.warning("Invalid UTF-8 in %s; replacing bad bytes with U+FFFD (Spark parity).", path)
        return _frame_any_scanner(_transcode_utf8(engine, _resolve_files(path)))


def _is_parallel_scanner_refusal(exc: BaseException) -> bool:
    """Return ``True`` for DuckDB's "Parallel CSV Reader … not support" error."""
    text = str(exc)
    return ("Parallel CSV Reader" in text and "parallel = false" in text) or (
        "parallel scanner does not support" in text.lower()
    )


def _is_strict_rejection(exc: BaseException) -> bool:
    """Return ``True`` when a strict-mode parse failed on the *shape* of the data.

    Strict mode raises ``Invalid Input Error: CSV Error`` for ragged /
    malformed rows (which the lenient parse pads or repairs), and the
    parallel scanner may still refuse the file. Encoding errors are
    *not* strict rejections — they bubble up to the U+FFFD transcode.
    """
    text = str(exc)
    if "unicode" in text.lower() or "utf-8" in text.lower():
        return False
    return "CSV Error" in text or _is_parallel_scanner_refusal(exc)


def _transcode_utf8(engine: Any, files: list[str]) -> list[str]:
    """Copy ``files`` into scratch as valid UTF-8 (``errors="replace"``)."""
    out_dir = local_scratch_dir(engine)
    out: list[str] = []
    for i, src in enumerate(files):
        dst = os.path.join(out_dir, f"{i:05d}-{os.path.basename(src)}")
        with (
            open(src, encoding="utf-8", errors="replace", newline="") as fin,
            open(dst, "w", encoding="utf-8", newline="") as fout,
        ):
            shutil.copyfileobj(fin, fout, length=16 * 1024 * 1024)
        out.append(dst)
    return out


def parquet_glob(path: Any) -> Any:
    """Turn a Spark Parquet directory into a glob DuckDB understands."""
    if isinstance(path, (list, tuple)):
        return [parquet_glob(p) for p in path]
    p = str(path)
    if os.path.isdir(p):
        return os.path.join(p, "**", "*.parquet")
    return p


# ---------------------------------------------------------------------------
# CSV write (Spark disc writer contract)
# ---------------------------------------------------------------------------


def _shortest_double_text(conn: Any) -> None:
    """Register a macro rendering doubles via DuckDB's shortest repr (Java parity)."""
    conn.execute(
        "CREATE OR REPLACE TEMP MACRO __ovid2_java_double(x) AS ("
        "CASE WHEN x IS NULL THEN NULL "
        "WHEN isnan(x) THEN 'NaN' "
        "WHEN isinf(x) THEN CASE WHEN x > 0 THEN 'Infinity' ELSE '-Infinity' END "
        "WHEN x = 0 THEN CASE WHEN CAST(x AS VARCHAR) LIKE '-%' THEN '-0.0' ELSE '0.0' END "
        "ELSE list_transform([regexp_extract(CAST(x AS VARCHAR), "
        "'^(-?)(\\d+)(?:\\.(\\d+))?(?:e([+-]?\\d+))?$', ['s','i','f','e'])], "
        "lambda m: list_transform([CAST(COALESCE(NULLIF(m.e, ''), '0') AS INTEGER) + length(ltrim(m.i, '0')) - 1 "
        "  - CASE WHEN ltrim(m.i, '0') = '' THEN length(m.f) - length(ltrim(m.f, '0')) ELSE 0 END], "
        "  lambda exp10: list_transform([ltrim(m.i || m.f, '0')], lambda digits: "
        "    CASE WHEN exp10 >= -3 AND exp10 < 7 THEN "
        "      m.s || CASE WHEN exp10 >= 0 THEN "
        "        rpad(digits, CAST(exp10 + 1 AS INTEGER), '0')[1:exp10 + 1] || '.' || "
        "        COALESCE(NULLIF(rtrim(digits[exp10 + 2:], '0'), ''), '0') "
        "      ELSE '0.' || repeat('0', CAST(-exp10 - 1 AS BIGINT)) || rtrim(digits, '0') END "
        "    ELSE m.s || digits[1:1] || '.' || COALESCE(NULLIF(rtrim(digits[2:], '0'), ''), '0') "
        "      || 'E' || CAST(exp10 AS VARCHAR) END)[1])[1])[1] END)"
    )


#: DuckDB types whose text rendering can never contain a delimiter, a
#: line break, or be the empty string — they need no Univocity wrapping.
_SAFE_TEXT_TYPES: frozenset[str] = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "DOUBLE",
        "FLOAT",
        "REAL",
        "BOOLEAN",
        "DATE",
        "TIME",
        "UUID",
    }
)


#: Delimiters that can never occur in a typed column's text rendering
#: (digits, sign, ``.``, ``:``, ``-``, ``E``, ``T``/``Z``, NaN/Infinity, true/false).
_SAFE_TEXT_DELIMITERS: frozenset[str] = frozenset({"\t", ",", "|", ";", "\x01"})


def _is_safe_text_type(duck_type: str, delimiter: str) -> bool:
    """Return whether a ``duck_type`` value's text never needs wrapping."""
    if delimiter not in _SAFE_TEXT_DELIMITERS:
        return False
    t = duck_type.upper()
    return t in _SAFE_TEXT_TYPES or t.startswith(("DECIMAL", "TIMESTAMP"))


def _text_projection(frame: DuckFrame, delimiter: str) -> str:
    """Project every column to Spark's CSV text rendering.

    NULL stays NULL (``COPY … NULL ''`` writes it as empty). Only
    columns whose text can contain the delimiter / a line break / be
    ``''`` (strings and unknown types) get the Univocity wrapping; typed
    columns are cast straight to text. The wrapping is a plain ``CASE``
    (no per-cell ``list_transform``), so the row renderer stays cheap.
    """
    items: list[str] = []
    delim = sql_string(delimiter)
    for idx, (name, duck_type) in enumerate(frame.dtypes, start=1):
        ref = f"#{idx}"
        t = duck_type.upper()
        if t in ("DOUBLE", "FLOAT", "REAL"):
            text = f"__ovid2_java_double(CAST({ref} AS DOUBLE))"
        elif t == "BOOLEAN":
            text = f"CASE WHEN {ref} THEN 'true' WHEN NOT {ref} THEN 'false' END"
        elif t.startswith("TIMESTAMP"):
            # Spark CSV default timestampFormat: yyyy-MM-dd'T'HH:mm:ss.SSSXXX
            text = f"strftime(CAST({ref} AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S.%g') || 'Z'"
        elif t == "DATE":
            text = f"strftime({ref}, '%Y-%m-%d')"
        else:
            text = f"CAST({ref} AS VARCHAR)"
        if _is_safe_text_type(t, delimiter):
            items.append(f"{text} AS {ident(name)}")
            continue
        # Univocity (quote disabled -> '\0') wraps a value containing the
        # delimiter or a line break in NUL characters; Spark's "empty
        # value" for a non-null '' is '""'.
        v = text if t.startswith("VARCHAR") else f"({text})"
        items.append(
            f"CASE WHEN {v} = '' THEN '\"\"' "
            f"WHEN contains({v}, {delim}) OR contains({v}, chr(10)) OR contains({v}, chr(13)) "
            f"THEN chr(0) || replace({v}, chr(0), '\\' || chr(0)) || chr(0) "
            f"ELSE {v} END AS {ident(name)}"
        )
    return ", ".join(items)


def _header_bytes(columns: list[str], delimiter: str) -> bytes:
    def _render(name: str) -> str:
        if delimiter in name or "\n" in name or "\r" in name:
            return f"\x00{name}\x00"
        return name

    return (delimiter.join(_render(c) for c in columns) + "\n").encode("utf-8")


def write_csv_file(
    engine: DuckDBEngine,
    frame: DuckFrame,
    target: str,
    *,
    delimiter: str,
    header: bool = True,
) -> int:
    """Execute ``frame`` into one CSV file at ``target`` (Spark disc format).

    Rows are rendered to text in SQL (so quoting / number formatting
    match Spark byte-for-byte) and streamed by DuckDB's ``COPY`` with
    quoting disabled. The file is written to a temp sibling and renamed,
    so readers never see a partial file.

    Returns:
        The number of rows written.
    """
    conn = engine.connection()
    _shortest_double_text(conn)
    projection = _text_projection(frame, delimiter)
    text_rel = frame.relation.project(projection) if frame.columns else frame.relation
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    # DuckDB's own header is byte-identical to Spark's unless a column
    # name needs Univocity NUL-wrapping; only then is the header written
    # separately (one extra copy of the body).
    native_header = header and not any(
        delimiter in c or "\n" in c or "\r" in c for c in frame.columns
    )
    tmp_body = f"{target}.ovid2-body-{uuid.uuid4().hex[:8]}"
    view = f"__ovid2_out_{uuid.uuid4().hex[:8]}"
    text_rel.create_view(view, replace=True)
    try:
        result = conn.execute(
            f"COPY (SELECT * FROM {ident(view)}) TO {sql_string(tmp_body)} "
            f"(FORMAT csv, HEADER {'true' if native_header else 'false'}, "
            f"DELIMITER {sql_string(delimiter)}, QUOTE '', ESCAPE '', "
            "NULL '', PRESERVE_ORDER false)"
        ).fetchone()  # noqa: OVID2001 — COPY's own row-count result, no re-scan
        # ``COPY`` returns the rows written; a value containing a line
        # break is NUL-wrapped, so a newline count would over-count anyway.
        rows = int(result[0]) if result else _count_lines(tmp_body) - int(native_header)
        if header and not native_header:
            tmp_final = f"{target}.ovid2-tmp-{uuid.uuid4().hex[:8]}"
            with open(tmp_final, "wb") as out:
                out.write(_header_bytes(frame.columns, delimiter))
                out.flush()
                with open(tmp_body, "rb") as body:
                    _append_file(body, out)
        else:
            tmp_final = tmp_body
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.replace(tmp_final, target)
    finally:
        with contextlib.suppress(Exception):
            conn.execute(f"DROP VIEW IF EXISTS {ident(view)}")
        with contextlib.suppress(OSError):
            os.unlink(tmp_body)
    return rows


def _append_file(src: Any, dst: Any) -> None:
    """Append ``src`` to ``dst`` in kernel space (``copy_file_range``) when possible."""
    size = os.fstat(src.fileno()).st_size
    offset = 0
    copy_range = getattr(os, "copy_file_range", None)
    if copy_range is not None:
        try:
            while offset < size:
                n = copy_range(src.fileno(), dst.fileno(), size - offset, offset)
                if n <= 0:
                    break
                offset += n
        except OSError:
            pass
    if offset < size:
        src.seek(offset)
        dst.seek(0, os.SEEK_END)
        shutil.copyfileobj(src, dst, length=16 * 1024 * 1024)


def _count_lines(path: str) -> int:
    count = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(16 * 1024 * 1024):
            count += chunk.count(b"\n")  # noqa: OVID2001 — bytes.count, not a DataFrame action
    return count


def write_frame_local(
    engine: DuckDBEngine,
    frame: DuckFrame,
    root: str,
    table_name: str,
    *,
    fmt: str,
    delimiter: str,
    single_file: bool,
) -> str:
    """Write ``frame`` under ``root`` as ``{table}.{fmt}`` (disc layout).

    * CSV, ``single_file=True``: one ``{table}.csv`` file.
    * CSV, ``single_file=False``: a ``{table}.csv/`` directory with one
      ``part-00000.csv`` (header included), the Spark part-dir shape.
    * Parquet: a ``{table}.parquet/`` directory with
      ``part-00000.parquet`` (Spark readers accept it).
    * JSON: ``{table}.json/part-00000.json`` (newline-delimited).

    Returns:
        The written artefact path.
    """
    Path(root).mkdir(parents=True, exist_ok=True)
    target = os.path.join(root, f"{table_name}.{fmt}")
    conn = engine.connection()
    if fmt == "csv":
        if single_file:
            engine.add_rows_written(write_csv_file(engine, frame, target, delimiter=delimiter))
            return target
        work = f"{target}.ovid2-tmp-{uuid.uuid4().hex[:8]}"
        os.makedirs(work)
        engine.add_rows_written(
            write_csv_file(engine, frame, os.path.join(work, "part-00000.csv"), delimiter=delimiter)
        )
        _swap_dir(work, target)
        return target
    if fmt in ("parquet", "json"):
        work = f"{target}.ovid2-tmp-{uuid.uuid4().hex[:8]}"
        os.makedirs(work)
        view = f"__ovid2_out_{uuid.uuid4().hex[:8]}"
        frame.relation.create_view(view, replace=True)
        try:
            out = os.path.join(work, f"part-00000.{fmt}")
            options = "FORMAT parquet, COMPRESSION snappy" if fmt == "parquet" else "FORMAT json"
            conn.execute(f"COPY (SELECT * FROM {ident(view)}) TO {sql_string(out)} ({options})")
            open(os.path.join(work, "_SUCCESS"), "wb").close()
        finally:
            with contextlib.suppress(Exception):
                conn.execute(f"DROP VIEW IF EXISTS {ident(view)}")
        _swap_dir(work, target)
        return target
    raise ValueError(f"Unsupported format: {fmt}")


def _swap_dir(work: str, target: str) -> None:
    """Replace ``target`` (file or dir) with the finished ``work`` dir."""
    if os.path.isdir(target):
        trash = f"{target}.ovid2-old-{uuid.uuid4().hex[:8]}"
        os.rename(target, trash)
        os.rename(work, target)
        shutil.rmtree(trash, ignore_errors=True)
    else:
        if os.path.exists(target):
            os.unlink(target)
        os.rename(work, target)


def local_scratch_dir(engine: DuckDBEngine) -> str:
    """Return a fresh scratch directory under the engine's temp dir."""
    engine.connection()
    base = engine._temp_dir or tempfile.gettempdir()
    path = os.path.join(base, f"stage-{uuid.uuid4().hex[:10]}")
    os.makedirs(path, exist_ok=True)
    return path
