"""Run a pipeline's tables — sequentially, or in parallel on DuckDB.

Every engine runs the CDM's tables through :func:`run_pipeline`. Spark
and Snowpark keep the historical one-table-at-a-time loop. DuckDB runs
in-process, and a single table rarely keeps its whole thread pool busy
(CSV text writes, ``DISTINCT ON``, small tables), so with an engine that
offers :meth:`table_worker` / :meth:`table_concurrency` the run is
split into two phases:

* **Phase A — reference tables, sequential, CDM order.** Cross-table
  fixers of later tables read the CDM's ``reference_tables``
  (``demographic`` / ``provider`` / ``harvest`` / ``encounter`` /
  ``death`` for PCORnet) by name; a missing reference silently skips the
  fix. They run first, one at a time, each with the whole pool.
* **Phase B — every other table, in parallel, largest first.** Each
  table runs on its own DuckDB cursor. Tables are started in descending
  source size (storage metadata only — :meth:`Driver.table_sizes`, no
  scan), the longest-processing-time-first heuristic, so a large table
  never waits at the back of the queue while small ones finish.

The runner is lazy-safe (AGENTS.md §1): it never executes a DataFrame;
size lookup is a file ``stat`` / object listing.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: ``(table) -> None``; raises on failure. Loads the formatter and runs the table.
TableJob = Callable[[str], None]
#: ``(table, exception) -> None``; called once per failed table.
FailureHook = Callable[[str, BaseException], None]


@dataclass
class TableRunResult:
    """Outcome of :func:`run_tables`.

    Attributes:
        succeeded: Tables that completed, in completion order.
        failed: ``{table: exception}`` for tables that raised.
        order: The order tables were *started* in.
        concurrency: Tables run at once in the parallel phase (1 = sequential).
    """

    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, BaseException] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    concurrency: int = 1


def supports_parallel_tables(engine: Any) -> bool:
    """Return ``True`` when ``engine`` can run tables on parallel workers."""
    return callable(getattr(engine, "table_worker", None)) and callable(
        getattr(engine, "table_concurrency", None)
    )


def split_reference_tables(
    tables: Sequence[str], reference_tables: Collection[str]
) -> tuple[list[str], list[str]]:
    """Split ``tables`` into ``(reference, other)``, keeping input order in each."""
    refs = {t.lower() for t in reference_tables}
    head = [t for t in tables if t.lower() in refs]
    tail = [t for t in tables if t.lower() not in refs]
    return head, tail


def order_largest_first(tables: Sequence[str], sizes: dict[str, int]) -> list[str]:
    """Return ``tables`` sorted by ``sizes`` descending (stable; unknown sizes last)."""
    indexed = list(enumerate(tables))
    indexed.sort(key=lambda p: (0 if p[1] in sizes else 1, -sizes.get(p[1], 0), p[0]))
    return [t for _, t in indexed]


def run_tables(
    tables: Sequence[str],
    run_one: TableJob,
    *,
    engine: Any,
    reference_tables: Collection[str] = (),
    table_sizes: Callable[[list[str]], dict[str, int]] | None = None,
    on_failure: FailureHook | None = None,
    fail_fast: bool = False,
    prestage: Callable[[list[str], int], None] | None = None,
) -> TableRunResult:
    """Run every table in ``tables`` through ``run_one``.

    A table failure is recorded (``on_failure``) and the run continues,
    matching the sequential loop. ``KeyboardInterrupt`` (cooperative
    cancel) stops scheduling, cancels queued tables, interrupts running
    queries, and propagates.

    Args:
        tables: Tables in CDM order.
        run_one: Runs one table; raises on failure.
        engine: The active engine. Parallel only when it supports
            :func:`supports_parallel_tables` and its
            ``table_concurrency`` exceeds 1.
        reference_tables: Tables later tables read by name (phase A).
        table_sizes: ``(tables) -> {table: bytes}``; best-effort, any
            failure keeps CDM order.
        on_failure: Called with ``(table, exc)`` for each failed table.
        fail_fast: Stop starting new tables after the first failure
            (tables already running finish). The scheduler's historical
            contract; ``ovid2 etl`` keeps going.
        prestage: ``(tail_tables, concurrency) -> None``; called once
            after the reference tables and before the parallel phase so
            the engine can parse every source of the remaining tables up
            front, in parallel, largest first (see
            :func:`prestage_sources`). Best-effort: a failure is logged
            and each table stages its own sources as before.

    Returns:
        A :class:`TableRunResult`.
    """
    result = TableRunResult()
    stop = threading.Event()

    def _one(table: str) -> None:
        if stop.is_set():
            return
        try:
            run_one(table)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            result.failed[table] = exc
            if fail_fast:
                stop.set()
            if on_failure is not None:
                on_failure(table, exc)
            return
        result.succeeded.append(table)

    tables = list(tables)
    concurrency = (
        int(engine.table_concurrency(len(tables))) if supports_parallel_tables(engine) else 1
    )
    if concurrency <= 1 or len(tables) < 2:
        result.order = list(tables)
        for table in tables:
            _one(table)
        return result

    head, tail = split_reference_tables(tables, reference_tables)
    sizes = _safe_sizes(table_sizes, tail)
    tail = order_largest_first(tail, sizes)
    concurrency = min(concurrency, max(1, len(tail)))
    result.order = head + tail
    result.concurrency = concurrency
    logger.info(
        "Parallel tables: %d reference table(s) first %s, then %d table(s) "
        "largest-first on %d worker(s): %s",
        len(head),
        head,
        len(tail),
        concurrency,
        [f"{t}({sizes[t]}B)" if t in sizes else t for t in tail],
    )

    # Phase A — reference tables, on the main thread (root connection).
    for table in head:
        _one(table)

    # Phase A' — parse every remaining source once, in parallel, before
    # the tables start (they then hit the stage cache).
    if prestage is not None and tail and not stop.is_set():
        try:
            prestage(list(tail), concurrency)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # advisory; tables stage on demand
            logger.warning("Source pre-staging failed; tables stage on demand: %s", exc)

    # Phase B — parallel, largest first. ``ThreadPoolExecutor`` starts
    # queued work FIFO, so submission order is start order.
    _run_parallel(tail, _one, engine=engine, concurrency=concurrency, stop=stop)
    return result


def _run_parallel(
    tables: list[str],
    one: Callable[[str], None],
    *,
    engine: Any,
    concurrency: int,
    stop: threading.Event,
) -> None:
    """Run ``tables`` on ``concurrency`` worker threads, each on its own cursor."""
    if not tables:
        return

    def _worker(table: str) -> None:
        if stop.is_set():
            return
        with engine.table_worker():
            one(table)

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ovid2-table")
    futures: list[Future[None]] = []
    try:
        # Worker threads start with an empty context; copy the caller's so
        # the active-engine ContextVar (``dsl.use_engine``) carries over.
        futures = [pool.submit(contextvars.copy_context().run, _worker, t) for t in tables]
        pending = set(futures)
        while pending:
            # Short timeout so the main thread stays responsive to signals.
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()  # re-raise KeyboardInterrupt from a worker
    except BaseException:
        stop.set()
        for future in futures:
            future.cancel()
        cancel = getattr(engine, "cancel_running_queries", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as exc:
                logger.warning("cancel_running_queries failed: %s", exc)
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def prestage_sources(
    engine: Any,
    formatters: dict[str, Any],
    stage_one: Callable[[str], None],
    *,
    sizes: Callable[[list[str]], dict[str, int]] | None = None,
) -> Callable[[list[str], int], None]:
    """Return a ``run_tables(prestage=...)`` hook staging sources in parallel.

    Collects each tail table's :attr:`~ovid2.formatters.base.BaseFormatter.source_globs`,
    de-duplicates them, orders them largest first (``sizes`` maps glob →
    bytes) and calls ``stage_one(glob)`` on ``concurrency`` worker
    threads, each on its own engine cursor. ``stage_one`` must be
    idempotent with the tables' own reads (the DuckDB stage cache makes
    the later per-table read a cache hit). A glob that fails to stage is
    logged and left for its table to report.
    """

    def _hook(tables: list[str], concurrency: int) -> None:
        globs: list[str] = []
        for table in tables:
            for glob in getattr(formatters.get(table), "source_globs", None) or []:
                if glob and glob != "*" and glob not in globs:
                    globs.append(glob)
        if not globs:
            return
        by_size = _safe_sizes(sizes, globs) if sizes is not None else {}
        globs.sort(key=lambda g: -by_size.get(g, 0))
        logger.info("Pre-staging %d source(s) on %d worker(s): %s", len(globs), concurrency, globs)

        def _stage(glob: str) -> None:
            try:
                stage_one(glob)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.info("Pre-stage of %s skipped (%s); its table will report it.", glob, exc)

        _run_parallel(
            globs,
            _stage,
            engine=engine,
            concurrency=max(1, min(concurrency, len(globs))),
            stop=threading.Event(),
        )

    return _hook


def driver_prestage(
    engine: Any,
    address_name: str | None,
    formatters: dict[str, Any],
    *,
    pipeline: str,
    batch_id: str,
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> Callable[[list[str], int], None] | None:
    """Return a :func:`prestage_sources` hook for a staged engine, else ``None``.

    Only engines that stage text sources into a shared cache (DuckDB:
    ``stage_sources_enabled()``) benefit; Spark / Snowpark return
    ``None`` and run exactly as before.
    """
    enabled = getattr(engine, "stage_sources_enabled", None)
    if not address_name or not callable(enabled) or not enabled():
        return None
    # Opt-in: measured on ACME (24 tables, 96 cores) a barrier that stages
    # every source before any table starts was *slower* (589 s vs 474 s)
    # than letting tables stage on demand — one serial-parse source
    # (drug_exposure.csv) holds every table back. Kept for feeds where
    # many tables share a few large sources.
    flag = os.environ.get("OVID2_DUCKDB_PRESTAGE", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None
    from ovid2.drivers.registry import driver_from_address

    driver = driver_from_address(
        address_name, pipeline=pipeline, sf_overrides=sf_overrides, cdm_name=cdm_name
    )
    stager = engine.get_stager()

    def _stage_one(glob: str) -> None:
        stager.materialise(driver, pipeline, batch_id, glob)

    def _sizes(globs: list[str]) -> dict[str, int]:
        return dict(driver.table_sizes(sorted(set(globs)), pipeline=pipeline, batch_id=batch_id))

    return prestage_sources(engine, formatters, _stage_one, sizes=_sizes)


def _safe_sizes(
    table_sizes: Callable[[list[str]], dict[str, int]] | None, tables: list[str]
) -> dict[str, int]:
    """Return ``table_sizes(tables)``, or ``{}`` on any failure (advisory)."""
    if table_sizes is None or len(tables) < 2:
        return {}
    try:
        raw = table_sizes(list(tables))
    except Exception as exc:  # scheduling is advisory; never fail the run
        logger.warning("Could not read table sizes; keeping CDM order: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, int) and v >= 0}


def source_sizes_from_driver(
    address_name: str | None,
    formatters: dict[str, Any],
    *,
    pipeline: str,
    batch_id: str,
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> Callable[[list[str]], dict[str, int]]:
    """Return a ``table_sizes`` callable backed by the input driver's metadata.

    Each table's size is the summed size of its formatter's
    ``input_file_name`` (glob) under the input address. Custom
    formatters fanning in several sources (PCORnet->OMOP ``measurement``)
    report their primary source, which is a good-enough ordering key.

    Args:
        address_name: Input address name (``None`` → no sizes).
        formatters: ``{table: formatter}`` already loaded for the run.
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        sf_overrides: Legacy Snowflake overrides.
        cdm_name: Active CDM.
    """

    def _sizes(tables: list[str]) -> dict[str, int]:
        if not address_name:
            return {}
        from ovid2.drivers.registry import driver_from_address

        driver = driver_from_address(
            address_name, pipeline=pipeline, sf_overrides=sf_overrides, cdm_name=cdm_name
        )
        file_for: dict[str, str] = {}
        for table in tables:
            formatter = formatters.get(table)
            name = str(getattr(formatter, "input_file_name", "") or "")
            if name and name != "*":
                file_for[table] = name
        if not file_for:
            return {}
        by_file = driver.table_sizes(
            sorted(set(file_for.values())), pipeline=pipeline, batch_id=batch_id
        )
        return {t: by_file[f] for t, f in file_for.items() if f in by_file}

    return _sizes


__all__ = [
    "TableRunResult",
    "driver_prestage",
    "order_largest_first",
    "prestage_sources",
    "run_tables",
    "source_sizes_from_driver",
    "split_reference_tables",
    "supports_parallel_tables",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.core.table_runner is a library module; run `ovid2 etl`.")
