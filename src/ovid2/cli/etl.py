"""``ovid2 etl`` subcommand implementation.

Two modes:

- **Host mode** (outside Docker): builds a ``docker run spark-submit`` and
  optionally starts / tears down an isolated Spark cluster.
- **Container mode** (inside Docker): runs the pipeline directly against the
  Spark session created by :mod:`ovid2.core.engines.pyspark`.

The subcommand is registered by :mod:`ovid2.cli.__main__`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ovid2.config.settings import DEFAULT_SF_ACCOUNT
from ovid2.core.docker import is_inside_docker, submit_to_docker, submit_with_cluster
from ovid2.core.events import (
    instance_finished,
    instance_started,
    set_default_bus,
    table_failed,
)
from ovid2.core.isolation import OvidInstance, cleanup_stale_instances
from ovid2.core.loaders import (
    load_address,
    load_engine,
    load_formatter,
    load_transformations,
    resolve_tables,
    resolve_transformations,
)
from ovid2.core.pipeline import register_shared_tables, run_pipeline
from ovid2.display.bus import JsonlEventBus, TeeEventBus
from ovid2.display.renderers.plain_log import PlainLogBus

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``etl`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "etl",
        help="Run the ETL pipeline for one or more folders.",
        description=(
            "Run the format → deduplicate → map → fix → write pipeline "
            "for one or more CDM tables of a pipeline."
        ),
    )
    parser.set_defaults(_command="etl", _entry=main)

    parser.add_argument("--input_address", required=True, help="Name of the input address config.")
    parser.add_argument(
        "--output_address", required=True, help="Name of the output address config."
    )
    parser.add_argument("--pipeline", required=True, help="Pipeline name (e.g. 'demo').")
    parser.add_argument("--batch_id", required=True, help="Batch identifier (e.g. '20260101').")
    parser.add_argument(
        "--tables",
        nargs="+",
        default=["all"],
        help="Tables to process, or 'all' to expand from pipeline_settings.",
    )
    parser.add_argument(
        "--transformations",
        nargs="+",
        default=["all"],
        help="Transformations to run in canonical order.",
    )
    parser.add_argument("--engine", default="pyspark", help="Compute engine (default: pyspark).")
    parser.add_argument(
        "--workers",
        type=int,
        default=25,
        help="Number of Spark workers when starting an isolated cluster.",
    )
    # Snowflake overrides (highest tier of the four-tier resolution).
    parser.add_argument("--sf_database", default=None, help="Snowflake DATABASE override.")
    parser.add_argument("--sf_account", default=None, help="Snowflake ACCOUNT override.")
    parser.add_argument("--sf_schema", default=None, help="Snowflake SCHEMA override.")
    parser.add_argument(
        "--cdm",
        default=None,
        help=(
            "CDM name (e.g. 'pcornet_7_0'). Overrides the CDM the pipeline would "
            "otherwise resolve to via `pipeline_settings.cdm`. Consumed by the "
            "Snowflake write path to pick the right typed DDL. Introduced by C1."
        ),
    )
    parser.add_argument(
        "--stderr-log",
        action="store_true",
        help="Mirror every event to stderr as a plain-text log line.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help=(
            "Run the pipeline in-process on the host, bypassing Docker. "
            "Requires the [spark] extra + a JDK for the default pyspark "
            "engine (a local[*] Spark session is created). Intended for "
            "development, testing, and small disc→disc runs. Equivalent to "
            "setting OVID2_EXECUTION_MODE=inprocess."
        ),
    )


def _sf_overrides(args: argparse.Namespace) -> dict[str, str] | None:
    """Collect any ``--sf_*`` CLI values into an override dict."""
    overrides: dict[str, str] = {}
    if args.sf_database:
        overrides["database"] = args.sf_database
    if args.sf_account:
        overrides["account"] = args.sf_account
    if args.sf_schema:
        overrides["schema"] = args.sf_schema
    return overrides or None


def _wants_inprocess(args: argparse.Namespace) -> bool:
    """Return ``True`` when the run should execute in-process (no Docker).

    Two switches enable the host-side in-process path:

    - ``--local`` on the CLI (``args.local``).
    - ``OVID2_EXECUTION_MODE=inprocess`` in the environment.

    Either one routes ``ovid2 etl`` straight to :func:`_run_container_mode`
    on the host, bypassing the ``docker run`` / ``docker compose`` dispatch.
    The pipeline itself is Docker-agnostic; only the host dispatch shell
    assumes containers.

    Args:
        args: Parsed CLI namespace.

    Returns:
        ``True`` if the pipeline should run in-process on the host.
    """
    if getattr(args, "local", False):
        return True
    return os.environ.get("OVID2_EXECUTION_MODE", "").strip().lower() == "inprocess"


def main(args: argparse.Namespace) -> int:
    """Entry point invoked by :mod:`ovid2.cli.__main__`.

    Args:
        args: Parsed CLI namespace with all ``etl`` options.

    Returns:
        Process exit code.
    """
    # Engine/driver family compatibility. Refuses ``--engine snowpark``
    # paired with a non-Snowflake input or output before any Spark
    # cluster is spun up (host mode) or any Snowpark session is built
    # (container mode). Runs on both sides: the host-side call saves
    # the 25-container cluster-start cost; the container-side call is
    # belt+suspenders for direct-in-container invocations. See
    # :mod:`ovid2.drivers.engine_compat` for the historical failure
    # mode this closes. The equivalent check on ``ovid2 tools copy`` /
    # ``ovid2 tools move`` uses the loaded-engine variant; here we
    # use the type-name variant so the abort happens before any
    # ``load_engine`` cost (which is minimal for pyspark but non-
    # trivial for snowpark).
    try:
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )
        assert_engine_type_supports_drivers(
            args.engine,
            [args.input_address, args.output_address],
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("engine_compat check skipped: %s", exc)

    # In-process bypass (``--local`` / OVID2_EXECUTION_MODE=inprocess):
    # run the pipeline directly on the host without Docker. This reaches
    # the same code path as container mode; it is the honest local
    # quick-start entry point (see docs/quickstart-local.md).
    if _wants_inprocess(args):
        return _run_container_mode(args)
    if not is_inside_docker():
        return _run_host_mode(args)
    return _run_container_mode(args)


def _run_host_mode(args: argparse.Namespace) -> int:
    """Dispatch the run to Docker (with or without an isolated cluster)."""
    cleanup_stale_instances()
    instance = OvidInstance()
    logger.info("Ovid2 host mode: instance %s", instance.instance_id)

    # By default the ``spark_mode`` in settings decides. Cluster mode is
    # heavier but faster on large batches; local mode is single-container.
    from ovid2.config.settings import SPARK_MODE

    if not getattr(args, "command", None):
        args.command = "etl"  # required by build_docker_command()

    # Auto-mount disc-connection roots for the input + output
    # addresses. Without this, an ETL job whose feed lives outside the
    # standard mount set (e.g. under /processing) would fail on the
    # container-side ``Path(...).open`` walk-up. ETL never accepts
    # ``--extra-mount`` from the operator, so ``parsed_extra_mounts``
    # here carries only the auto-derived list.
    from ovid2.connections.disc_mounts import build_disc_mounts
    args.parsed_extra_mounts = build_disc_mounts(
        [getattr(args, "input_address", None), getattr(args, "output_address", None)],
    )

    if SPARK_MODE == "cluster":
        return submit_with_cluster(args, instance)
    return submit_to_docker(args, instance)


def _run_container_mode(args: argparse.Namespace) -> int:
    """Run the pipeline directly, inside the Spark container."""
    pipeline = args.pipeline
    batch_id = args.batch_id

    tables = resolve_tables(args.tables, pipeline)
    transformation_names = resolve_transformations(args.transformations)
    transformers = load_transformations(transformation_names)

    # ------------------------------------------------------------------
    # Event bus setup — JSONL persist + optional stderr mirror.
    # ------------------------------------------------------------------
    instance_id = os.environ.get("OVID2_INSTANCE_ID", "")
    if not instance_id:
        instance_id = OvidInstance().instance_id
    from ovid2.config.settings import WORK_DIR

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    event_log = WORK_DIR / f"events-{instance_id}.jsonl"
    jsonl_bus = JsonlEventBus(event_log)
    bus: object = TeeEventBus(jsonl_bus, PlainLogBus()) if args.stderr_log else jsonl_bus
    set_default_bus(bus)  # type: ignore[arg-type]

    bus.emit(instance_started(instance_id=instance_id, pipeline=pipeline))

    engine = load_engine(args.engine)
    # Fail fast if the pipeline declares a different required family
    # (e.g. an DEMO custom formatter needs Spark but the user asked
    # for Snowpark). The check is a cheap import; no session is built.
    from ovid2.core.engines.family import assert_pipeline_engine_compatible

    assert_pipeline_engine_compatible(engine, pipeline)
    spark = engine.get_session()

    # Cooperative-cancel handler. When an operator cancels a run from
    # the web panel the worker delivers a SIGTERM to the host-side
    # ``ovid2`` process, which propagates into this container. For a
    # Snowpark run we take the extra step of aborting the queries in
    # flight on the live Snowflake session (SYSTEM$CANCEL_ALL_QUERIES)
    # before unwinding, so a cancelled run doesn't leave a long-running
    # warehouse query burning credits. For Spark, tearing the session
    # down in the ``finally`` block already stops the DAG. The handler
    # re-raises KeyboardInterrupt so the normal ``finally`` teardown
    # (sampler stop, engine.stop_session) still runs.
    import signal as _signal

    _cancel_seen = {"fired": False}

    def _on_cancel_signal(signum, _frame):  # type: ignore[no-untyped-def]
        if _cancel_seen["fired"]:
            return
        _cancel_seen["fired"] = True
        name = _signal.Signals(signum).name
        print(f"[ovid2] {name} received — cancelling run", file=sys.stderr)
        try:
            engine.cancel_running_queries()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_running_queries failed: %s", exc)
        raise KeyboardInterrupt(f"cancelled via {name}")

    _prev_sigterm = _signal.signal(_signal.SIGTERM, _on_cancel_signal)
    _prev_sigint = _signal.signal(_signal.SIGINT, _on_cancel_signal)

    # Bind a writable namespace for engine-managed temporary objects
    # (temp views / transient staging tables). No-op on Spark; on
    # Snowpark this binds a writable database + schema so temp views
    # don't land in the read-only SNOWFLAKE system database.
    engine.prepare_workspace()

    # Startup summary — the CLI is one of the packages allowed to print.
    print("[ovid2] ETL run")
    print(f"[ovid2]   pipeline={pipeline} batch_id={batch_id} tables={tables}")
    print(f"[ovid2]   transformations={transformation_names}")
    print(f"[ovid2]   engine={args.engine} instance_id={instance_id}")
    print(f"[ovid2]   event_log={event_log}")

    sf_overrides = _sf_overrides(args)
    # Phase C1: resolve the active CDM once here so both addresses see
    # the same value. Falls back to `pipeline_settings.cdm` (via the
    # registry helper) when `--cdm` is not passed.
    cdm_name = getattr(args, "cdm", None)
    if not cdm_name:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        cdm_name = resolve_cdm_for_pipeline(pipeline).name
    input_address = load_address(
        args.input_address,
        sf_overrides=sf_overrides,
        pipeline=pipeline,
        cdm_name=cdm_name,
    )
    output_address = load_address(
        args.output_address,
        sf_overrides=sf_overrides,
        pipeline=pipeline,
        cdm_name=cdm_name,
    )

    register_shared_tables(
        spark,  # type: ignore[arg-type]
        bus,  # type: ignore[arg-type]
        engine=engine,
        pipeline=pipeline,
        batch_id=batch_id,
        sf_overrides=sf_overrides,
    )
    # Start the live-metrics sampler as soon as the session is up.
    # Runs on a daemon thread, stops in the ``finally`` block below.
    from ovid2.core.metrics_sampler import run_sampler

    sampler = run_sampler(instance_id=instance_id, engine=engine, bus=bus)  # type: ignore[arg-type]

    failures = 0
    cancelled = False
    try:
        for table in tables:
            try:
                formatter = load_formatter(
                    pipeline,
                    table,
                    spark=spark,
                    input_address=input_address,
                    batch_id=batch_id,
                    engine=engine,
                    input_address_name=args.input_address,
                )
                run_pipeline(
                    engine=engine,
                    input_address=input_address,
                    output_address=output_address,
                    table_name=table,
                    formatter=formatter,
                    transformations=transformers,
                    pipeline=pipeline,
                    batch_id=batch_id,
                    event_bus=bus,  # type: ignore[arg-type]
                    instance_id=instance_id,
                    input_address_name=args.input_address,
                    output_address_name=args.output_address,
                    sf_overrides=sf_overrides,
                    cdm_name=cdm_name,
                )
            except Exception as exc:
                failures += 1
                logger.exception("Table '%s' failed: %s", table, exc)
                bus.emit(
                    table_failed(
                        instance_id=instance_id,
                        pipeline=pipeline,
                        batch_id=batch_id,
                        table=table,
                        error=str(exc),
                    )
                )
                print(f"[ovid2] ERROR table {table}: {exc}", file=sys.stderr)

        try:
            output_address.create_complementary_tables(spark, pipeline, batch_id)
        except Exception as exc:
            logger.exception("create_complementary_tables failed: %s", exc)
    except KeyboardInterrupt:
        # Cooperative cancel (SIGTERM/SIGINT). The signal handler has
        # already asked the engine to abort in-flight queries; here we
        # just record the run as unsuccessful and fall through to the
        # normal teardown + terminal event so the worker attributes the
        # exit to the cancel.
        cancelled = True
        print("[ovid2] Run cancelled", file=sys.stderr)
    finally:
        _signal.signal(_signal.SIGTERM, _prev_sigterm)
        _signal.signal(_signal.SIGINT, _prev_sigint)
        sampler.stop()
        engine.stop_session()

    success = failures == 0 and not cancelled
    print(
        f"[ovid2] Complete. failed={failures} "
        f"succeeded={len(tables) - failures}"
        + (" (cancelled)" if cancelled else "")
    )

    bus.emit(instance_finished(instance_id=instance_id, success=success))
    jsonl_bus.close()
    if cancelled:
        return 130  # 128 + SIGINT, conventional "terminated by signal"
    return 0 if failures == 0 else 1


# Suppress "unused import" warnings on symbols that are referenced only
# via ``getattr`` from the docker build (linter can't see them).
_ = DEFAULT_SF_ACCOUNT
