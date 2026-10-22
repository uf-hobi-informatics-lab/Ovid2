"""Batch scheduler service.

.. note::

   **LEGACY / PARKED (pipeline-not-pipeline Phase B, 2026-08-26).** This
   in-process folder-poller is superseded by the web-worker's ``watch``
   trigger path (:mod:`ovid2.web.services.watch_svc` +
   ``Worker._dispatch_watch_routes``), which is the live scheduling
   mechanism. This module is retained importable for rollback and for
   ``ovid2 scheduler`` (also documented as legacy) but is no longer
   wired as the primary scheduling path. Prefer creating a ``watch``
   pipeline on the web panel.

Polls pipeline data directories, registers new batches in the tracker,
and dispatches ETL runs when they are ready. Each dispatched run gets
its own :class:`~ovid2.core.isolation.OvidInstance` so per-invocation
scratch resources never collide with sibling runs.

The scheduler runs jobs **in-process** via :func:`ovid2.core.pipeline.run_pipeline`
by default. When ``launch_mode='subprocess'`` (the eventual production
setting), each slot spawns a fresh ``ovid2 etl`` subprocess with its own
Spark cluster — the invocation is prepared but not yet wired here so
the CLI stays testable without Docker.

State backend selection:

- Snowflake output address → :class:`SnowflakeBatchTracker`.
- Anything else            → :class:`SqliteBatchTracker` under
  ``$XDG_STATE_HOME/ovid2/scheduler.db``.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ovid2.config.settings import DATA_DIR
from ovid2.core.events import (
    Event,
    EventBus,
    get_default_bus,
)
from ovid2.core.isolation import OvidInstance
from ovid2.scheduler.batch_tracker import (
    STATUS_ERROR,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    BatchTracker,
    SnowflakeBatchTracker,
    SqliteBatchTracker,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_MINUTES: int = 15
DEFAULT_MIN_WAIT_MINUTES: int = 10
DEFAULT_MAX_WORKERS: int = 1


def _emit(bus: EventBus, kind: str, **payload: Any) -> None:
    """Emit a raw event on the bus with the given kind + payload."""
    bus.emit(Event(kind=kind, payload=payload))


class SchedulerService:
    """Automated batch processing scheduler.

    Args:
        engine: Compute engine (currently only PySpark is supported).
        input_address_name: Named input address config.
        output_address_name: Named output address config.
        poll_interval: Minutes between poll cycles.
        min_wait: Minutes a batch must have been waiting before dispatch.
        max_workers: Maximum concurrent ETL jobs.
        data_path: Root directory scanned for pipeline batches. Defaults
            to :data:`ovid2.config.settings.DATA_DIR`.
        tracker: Optional pre-built :class:`BatchTracker`. If omitted, the
            service auto-selects a backend based on the output address.
        event_bus: Optional event bus. Defaults to the process-wide bus.
    """

    def __init__(
        self,
        *,
        engine: Any,
        input_address_name: str,
        output_address_name: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL_MINUTES,
        min_wait: int = DEFAULT_MIN_WAIT_MINUTES,
        max_workers: int = DEFAULT_MAX_WORKERS,
        data_path: Path | None = None,
        tracker: BatchTracker | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.engine = engine
        self.input_address_name = input_address_name
        self.output_address_name = output_address_name
        self.poll_interval = int(poll_interval)
        self.min_wait = int(min_wait)
        self.max_workers = int(max_workers)
        self.data_path: Path = Path(data_path) if data_path is not None else DATA_DIR
        self._tracker: BatchTracker | None = tracker
        self._bus: EventBus = event_bus if event_bus is not None else get_default_bus()
        self._running: bool = False

    # ------------------------------------------------------------ tracker

    def _init_tracker(self) -> None:
        """Attach the appropriate tracker backend if not already set.

        Uses :class:`SnowflakeBatchTracker` when the output address is a
        :class:`SnowflakeAddress`; otherwise falls back to
        :class:`SqliteBatchTracker`.
        """
        if self._tracker is not None:
            return

        from ovid2.core.loaders import load_address

        try:
            output_address = load_address(self.output_address_name)
        except Exception as exc:
            logger.warning(
                "Could not load output address '%s' — using SQLite tracker (%s)",
                self.output_address_name,
                exc,
            )
            output_address = None

        # Import lazily so ``ovid2.drivers.snowflake`` isn't required.
        snowflake_cls: type[Any] | None
        try:
            from ovid2.drivers.snowflake import SnowflakeAddress

            snowflake_cls = SnowflakeAddress
        except ImportError:
            snowflake_cls = None

        if snowflake_cls is not None and isinstance(output_address, snowflake_cls):
            self._tracker = SnowflakeBatchTracker(sf_address=output_address)
        else:
            self._tracker = SqliteBatchTracker()

        try:
            self._tracker.initialize_tables()
        except Exception as exc:
            logger.error("Failed to initialise tracker tables: %s", exc)
            self._tracker = None

    # ---------------------------------------------------------- discovery

    def _known_pipelines(self) -> set[str]:
        """Return pipeline names discoverable via :mod:`ovid2_pipelines`."""
        try:
            import ovid2_pipelines  # noqa: F401
        except ModuleNotFoundError:
            return set()

        import sys

        module = sys.modules["ovid2_pipelines"]
        pipelines: set[str] = set()
        for base in getattr(module, "__path__", []):
            base_path = Path(base)
            if not base_path.is_dir():
                continue
            for entry in base_path.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("_") or entry.name == "__pycache__":
                    continue
                if (entry / "__init__.py").is_file():
                    pipelines.add(entry.name)
        return pipelines

    def scan_disc_for_batches(self) -> list[dict[str, str]]:
        """Scan ``data_path`` for pipeline batches.

        For each pipeline directory whose name matches a known pipeline
        (case-insensitive), yield every immediate subdirectory as a
        candidate batch.

        Returns:
            List of dicts with ``pipeline``, ``batch_id`` and ``path`` keys.
        """
        if not self.data_path.is_dir():
            logger.warning("Data path does not exist: %s", self.data_path)
            return []

        known = self._known_pipelines()
        batches: list[dict[str, str]] = []

        for entry in sorted(self.data_path.iterdir()):
            if not entry.is_dir():
                continue

            pipeline = self._match_pipeline(entry.name, known)
            if pipeline is None:
                continue

            for batch_dir in sorted(entry.iterdir()):
                if batch_dir.is_dir():
                    batches.append(
                        {
                            "pipeline": pipeline,
                            "batch_id": f"{entry.name}/{batch_dir.name}",
                            "path": str(batch_dir),
                        }
                    )
        return batches

    @staticmethod
    def _match_pipeline(dir_name: str, known: set[str]) -> str | None:
        """Return the canonical pipeline name for ``dir_name`` (case-insensitive)."""
        target = dir_name.lower()
        for candidate in known:
            if candidate.lower() == target:
                return candidate
        return None

    # -------------------------------------------------------- pending set

    def pending_batches(self) -> list[dict[str, str]]:
        """Return batches that should be dispatched this poll cycle.

        With a tracker: newly discovered batches are registered; the
        tracker's ``get_ready_batches`` filters by status + min_wait.

        Without a tracker (unusual — only when both backends refused
        initialisation): returns everything on disk.
        """
        disk_batches = self.scan_disc_for_batches()
        if self._tracker is None:
            return disk_batches

        by_batch_id = {b["batch_id"]: b for b in disk_batches}

        for batch in disk_batches:
            try:
                status = self._tracker.get_batch_status(batch["batch_id"])
            except Exception as exc:
                logger.error("Tracker lookup failed for '%s': %s", batch["batch_id"], exc)
                continue
            if status is None:
                try:
                    self._tracker.register_batch(batch["batch_id"], batch["path"])
                    _emit(
                        self._bus,
                        "batch.received",
                        pipeline=batch["pipeline"],
                        batch_id=batch["batch_id"],
                    )
                except Exception as exc:
                    logger.error("Failed to register '%s': %s", batch["batch_id"], exc)

        try:
            ready = self._tracker.get_ready_batches(self.min_wait)
        except Exception as exc:
            logger.error("get_ready_batches failed: %s", exc)
            return []

        pending: list[dict[str, str]] = []
        for row in ready:
            batch = by_batch_id.get(row["batch_id"])
            if batch is None:
                logger.warning(
                    "Batch '%s' is in tracker but no longer on disk — skipping",
                    row["batch_id"],
                )
                continue
            pending.append(batch)
        return pending

    # ----------------------------------------------------------- dispatch

    def run_etl_job(self, pipeline: str, batch_id: str) -> bool:
        """Execute a single ETL job in-process. Returns success flag.

        Each job creates its own :class:`OvidInstance` so any per-run
        scratch resources are namespaced separately from siblings.
        """
        from ovid2.core.loaders import (
            get_all_tables,
            get_all_transformations,
            load_address,
            load_formatter,
            load_transformations,
        )
        from ovid2.core.pipeline import run_pipeline

        instance = OvidInstance()
        _emit(
            self._bus,
            "batch.dispatched",
            pipeline=pipeline,
            batch_id=batch_id,
            instance_id=instance.instance_id,
        )
        if self._tracker is not None:
            try:
                self._tracker.update_status(
                    batch_id, STATUS_PROCESSING, timestamp_field="processing_start"
                )
            except Exception as exc:
                logger.error("Tracker update PROCESSING failed for '%s': %s", batch_id, exc)

        success = False
        try:
            input_address = load_address(self.input_address_name, pipeline=pipeline)
            output_address = load_address(self.output_address_name, pipeline=pipeline)
            spark = self.engine.get_session()
            tables = get_all_tables(pipeline)
            transformation_names = get_all_transformations()

            for table in tables:
                try:
                    formatter = load_formatter(
                        pipeline,
                        table,
                        spark=spark,
                        input_address=input_address,
                        batch_id=batch_id,
                    )
                except ValueError:
                    logger.warning(
                        "No formatter for pipeline '%s' table '%s' — skipping",
                        pipeline,
                        table,
                    )
                    continue
                transforms = load_transformations(transformation_names)
                run_pipeline(
                    engine=self.engine,
                    input_address=input_address,
                    output_address=output_address,
                    table_name=table,
                    formatter=formatter,
                    transformations=transforms,
                    pipeline=pipeline,
                    batch_id=batch_id,
                    event_bus=self._bus,
                    instance_id=instance.instance_id,
                    input_address_name=self.input_address_name,
                    output_address_name=self.output_address_name,
                )
            success = True
        except Exception as exc:
            logger.exception("ETL job failed for %s/%s", pipeline, batch_id)
            _emit(
                self._bus,
                "batch.failed",
                pipeline=pipeline,
                batch_id=batch_id,
                error=str(exc),
            )
        finally:
            try:
                instance.cleanup()
            except Exception as exc:
                logger.debug("Cleanup failed for %s: %s", instance.instance_id, exc)

        if self._tracker is not None:
            final_status = STATUS_PROCESSED if success else STATUS_ERROR
            try:
                self._tracker.update_status(
                    batch_id, final_status, timestamp_field="processing_end"
                )
            except Exception as exc:
                logger.error("Tracker update %s failed for '%s': %s", final_status, batch_id, exc)

        if success:
            _emit(self._bus, "batch.completed", pipeline=pipeline, batch_id=batch_id)
        return success

    # -------------------------------------------------------------- loop

    def run(self, *, once: bool = False) -> None:
        """Start polling. Blocks until :meth:`stop` is called.

        Args:
            once: When True, poll exactly once (useful for tests and
                one-shot invocations).
        """
        self._running = True
        self._init_tracker()

        mode = "stateful" if self._tracker is not None else "stateless"
        logger.info(
            "Scheduler starting (%s): poll_interval=%dm min_wait=%dm max_workers=%d",
            mode,
            self.poll_interval,
            self.min_wait,
            self.max_workers,
        )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while self._running:
                try:
                    self._poll_once(executor)
                except Exception as exc:
                    logger.error("Scheduler poll error: %s", exc, exc_info=True)

                if once:
                    self._running = False
                    break
                logger.debug("Sleeping %d minute(s)...", self.poll_interval)
                time.sleep(self.poll_interval * 60)

    def _poll_once(self, executor: ThreadPoolExecutor) -> None:
        """Dispatch one round of pending batches. Blocks until they all finish."""
        pending = self.pending_batches()
        if not pending:
            return
        logger.info("Dispatching %d pending batches", len(pending))
        futures: dict[Future[bool], dict[str, str]] = {
            executor.submit(self.run_etl_job, batch["pipeline"], batch["batch_id"]): batch
            for batch in pending
        }
        for future in as_completed(futures):
            batch = futures[future]
            success = future.result()
            logger.info("Batch '%s' %s", batch["batch_id"], "success" if success else "failed")

    def stop(self) -> None:
        """Ask the polling loop to exit at its next iteration."""
        self._running = False
        logger.info("Scheduler stop requested")


__all__ = ["SchedulerService"]
