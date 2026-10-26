"""``ovid2 scheduler`` — start the batch scheduler.

.. note::

   **LEGACY / PARKED (pipeline-not-pipeline Phase B, 2026-08-26).** The
   live scheduling path is now the web-worker's ``watch`` trigger (create
   a ``watch`` pipeline on the web panel; the ``ovid2 web-worker`` daemon
   polls each watched pipeline's input storage account and auto-submits
   new batches). This subcommand remains runnable for rollback but is no
   longer the recommended way to schedule runs.

Runs the polling loop as a long-lived host-side process. Each poll
dispatches new batches to a thread-pool of workers; every worker
executes one ETL job in-process. Batch state is persisted via
Snowflake or SQLite (auto-selected — see
:mod:`ovid2.scheduler.batch_tracker`).
"""

from __future__ import annotations

import argparse
import logging
import sys

from ovid2.core.events import get_default_bus, set_default_bus
from ovid2.core.isolation import OvidInstance
from ovid2.core.loaders import load_engine
from ovid2.display.bus import JsonlEventBus, TeeEventBus
from ovid2.display.renderers.plain_log import PlainLogBus
from ovid2.scheduler import SchedulerService

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``scheduler`` subcommand."""
    parser = subparsers.add_parser(
        "scheduler",
        help="Run the batch scheduler.",
        description=(
            "Poll a data directory for pipeline batches and dispatch ETL "
            "runs when they are ready. State is persisted to Snowflake or a "
            "local SQLite file."
        ),
    )
    parser.set_defaults(_command="scheduler", _entry=_entry)

    parser.add_argument("--input_address", required=True)
    parser.add_argument("--output_address", required=True)
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Minutes between poll cycles.",
    )
    parser.add_argument(
        "--min-wait",
        type=int,
        default=10,
        help="Minimum minutes a batch must wait before dispatch.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum concurrent ETL jobs.",
    )
    parser.add_argument(
        "--data-path",
        default=None,
        help="Root directory scanned for pipeline batches. Defaults to DATA_DIR.",
    )
    parser.add_argument("--engine", default="pyspark")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (useful for cron / systemd oneshot).",
    )
    parser.add_argument("--stderr-log", action="store_true")


def _entry(args: argparse.Namespace) -> int:
    """Start the scheduler polling loop."""
    from ovid2.config.settings import WORK_DIR

    instance = OvidInstance()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    event_log = WORK_DIR / f"events-{instance.instance_id}.jsonl"
    jsonl_bus = JsonlEventBus(event_log)
    bus = TeeEventBus(jsonl_bus, PlainLogBus()) if args.stderr_log else jsonl_bus
    set_default_bus(bus)

    print(f"[ovid2 scheduler] instance_id={instance.instance_id}")
    print(f"[ovid2 scheduler] event_log={event_log}")

    engine = load_engine(args.engine)
    service = SchedulerService(
        engine=engine,
        input_address_name=args.input_address,
        output_address_name=args.output_address,
        poll_interval=args.poll_interval,
        min_wait=args.min_wait,
        max_workers=args.max_workers,
        data_path=args.data_path,
        event_bus=get_default_bus(),
    )

    try:
        service.run(once=args.once)
    except KeyboardInterrupt:
        print("[ovid2 scheduler] Interrupt received, stopping…", file=sys.stderr)
        service.stop()
    finally:
        engine.stop_session()
        jsonl_bus.close()
    return 0
