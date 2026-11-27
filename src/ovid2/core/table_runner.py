"""Run a pipeline's tables — one table at a time, on every engine.

Every engine (PySpark, Databricks Spark, Snowpark, DuckDB) runs the CDM's
tables through :func:`run_pipeline` in the same way: sequentially, in CDM
order. One table at a time keeps engines consistent (the same start/finish
order, the same per-table timings, the same failure semantics) and gives
the running table the engine's *whole* capacity — DuckDB's full thread
pool and ``memory_limit``, the full Spark cluster, the whole Snowflake
warehouse — so each table finishes as fast as the engine allows.

The runner is lazy-safe (AGENTS.md §1): it never executes a DataFrame.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ovid2.core.cancellation import raise_if_cancelled

logger = logging.getLogger(__name__)

#: ``(table) -> None``; raises on failure. Loads the formatter and runs the table.
TableJob = Callable[[str], None]
#: ``(table, exception) -> None``; called once per failed table.
FailureHook = Callable[[str, BaseException], None]
#: ``() -> None``; called before every table (the operator-pause gate,
#: :func:`ovid2.core.pause.gate`). May block; raises on cancel.
BoundaryHook = Callable[[], None]

#: Tables run at once, on every engine. Pinned by
#: ``tests/contract/test_tables_run_serially.py``.
TABLE_CONCURRENCY: int = 1


@dataclass
class TableRunResult:
    """Outcome of :func:`run_tables`.

    Attributes:
        succeeded: Tables that completed, in completion order.
        failed: ``{table: exception}`` for tables that raised.
        order: The order tables were started in (CDM order).
    """

    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, BaseException] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def run_tables(
    tables: Sequence[str],
    run_one: TableJob,
    *,
    on_failure: FailureHook | None = None,
    fail_fast: bool = False,
    before_table: BoundaryHook | None = None,
) -> TableRunResult:
    """Run every table in ``tables`` through ``run_one``, one at a time.

    A table failure is recorded (``on_failure``) and the run continues
    unless ``fail_fast``. ``KeyboardInterrupt`` (cooperative cancel)
    propagates immediately.

    Args:
        tables: Tables in CDM order.
        run_one: Runs one table; raises on failure.
        on_failure: Called with ``(table, exc)`` for each failed table.
        fail_fast: Stop after the first failure (the scheduler's
            historical contract; ``ovid2 etl`` keeps going).
        before_table: Table-boundary hook run before each table — the
            operator pause (:func:`ovid2.core.pause.gate`) holds here, so
            a pause never interrupts a running table.

    Returns:
        A :class:`TableRunResult`.
    """
    result = TableRunResult(order=list(tables))
    for table in result.order:
        raise_if_cancelled()
        if before_table is not None:
            before_table()
        try:
            run_one(table)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # An engine may surface a cooperative cancel as an ordinary
            # error (DuckDB: "Query interrupted"); never keep going.
            raise_if_cancelled(exc)
            result.failed[table] = exc
            if on_failure is not None:
                on_failure(table, exc)
            if fail_fast:
                break
            continue
        result.succeeded.append(table)
    return result


__all__ = ["TABLE_CONCURRENCY", "BoundaryHook", "TableRunResult", "run_tables"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.core.table_runner is a library module; run `ovid2 etl`.")
