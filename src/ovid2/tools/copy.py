"""``ovid2 tools copy`` — one-shot data move between addresses.

Introduced by phase C1 as the non-scheduled counterpart to the
(intentionally rejected) ``passthrough`` route kind. Reads every named
table from ``--from`` and writes it to ``--to`` using the engine's
address contract — no formatters, no fixers, no CDM DDL. Schema is
inferred by the destination address.

Post-C1: exposed on the REST API and MCP as the ``copy`` route/job
kind so operators can trigger copies through the web submit form,
route dispatcher, or MCP tool without shelling into a container. When
invoked outside a container (via ``ovid2 tools copy …`` or the
web-worker's ``subprocess.Popen``) the CLI dispatches into a Docker
run via :func:`ovid2.core.docker.submit_to_docker` or
:func:`~ovid2.core.docker.submit_with_cluster` — same isolation
guarantees as ``ovid2 etl``.

Usage::

    ovid2 tools copy \\
        --from demo_disc --to demo_snowflake \\
        --pipeline demo --batch_id 20260316 \\
        --tables DEMOGRAPHIC ENCOUNTER DIAGNOSIS \\
        [--engine pyspark] \\
        [--extra-mount /host/path:/container/path[:rw|ro]] ...

Each ``--tables`` entry is either a bare logical table name
(``HASH_TOKEN``) or a ``LOGICAL=SOURCE`` split
(``HASH_TOKEN=*datavant_person_tokens*``). The LOGICAL half is always
used as the destination file stem (case preserved verbatim); the
SOURCE half, when present, is the source-side lookup key handed to
the driver's ``read()``. See :mod:`ovid2.tools.table_specs` for the
full grammar.

``--extra-mount`` is repeatable and lets the operator add host bind
mounts to the per-invocation container for cases where the
destination address points outside the standard mount set
(``/data``, ``/app``, ``/ovid2-user-config``, ``/ovid_credentials``,
``/usr/include/rsa``). Only ``ovid2 tools copy`` accepts the flag;
the ETL family (etl / analysis / dq_checks) is confined to the
standard mount set on purpose. See
:mod:`ovid2.tools.extra_mounts` for the validation rules.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ovid2.core.engines.stager import EngineStager
    from ovid2.core.events import EventBus
    from ovid2.drivers.base import Driver
    from ovid2.tools.table_specs import TableSpec

logger = logging.getLogger(__name__)

#: Default number of tables copied concurrently in container mode.
#:
#: Each per-table read is a single, non-splittable JDBC/CSV partition
#: (an Access ``.accdb`` has no natural partition column, so
#: :func:`ovid2.drivers.access.spark.read_table` yields exactly one
#: partition), which means one table keeps exactly one Spark executor
#: busy while the rest of the cluster idles. Submitting several tables'
#: ``persist`` actions concurrently from independent driver threads lets
#: Spark's scheduler run those single-task jobs in parallel across the
#: otherwise-idle workers — turning a serial 201-table copy into a
#: fan-out bounded by :data:`_COPY_TABLE_CONCURRENCY`. Overridable per
#: run via ``$OVID2_COPY_TABLE_CONCURRENCY``.
_DEFAULT_COPY_TABLE_CONCURRENCY: int = 8


def _copy_table_concurrency() -> int:
    """Return the per-table copy concurrency, honouring the env override.

    Reads ``$OVID2_COPY_TABLE_CONCURRENCY`` and falls back to
    :data:`_DEFAULT_COPY_TABLE_CONCURRENCY`. A value below 1 (or a
    non-integer) is clamped to 1 so a misconfiguration degrades to the
    original serial behaviour rather than crashing.
    """
    raw = os.environ.get("OVID2_COPY_TABLE_CONCURRENCY")
    if raw is None:
        return _DEFAULT_COPY_TABLE_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer OVID2_COPY_TABLE_CONCURRENCY=%r; falling back to %d.",
            raw,
            _DEFAULT_COPY_TABLE_CONCURRENCY,
        )
        return _DEFAULT_COPY_TABLE_CONCURRENCY
    return max(1, value)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tools copy`` subcommand."""
    parser = subparsers.add_parser(
        "copy",
        help="Copy tables from one address to another (one-shot, no CDM).",
        description=__doc__,
    )
    parser.add_argument(
        "--from", dest="from_address", required=True, help="Name of the source address config."
    )
    parser.add_argument(
        "--to", dest="to_address", required=True, help="Name of the destination address config."
    )
    parser.add_argument("--pipeline", required=True, help="Pipeline name.")
    parser.add_argument("--batch_id", required=True, help="Batch identifier.")
    parser.add_argument(
        "--tables",
        nargs="+",
        required=True,
        help=(
            "Table specs to copy. Each entry is either "
            "'LOGICAL' (source basename equals logical "
            "name) or 'LOGICAL=SOURCE' (LOGICAL is the "
            "destination file stem; SOURCE is the "
            "source-side lookup key, e.g. a disc glob "
            "'*datavant_person_tokens*' or a "
            "Snowflake dbtable). LOGICAL is always "
            "used verbatim as the destination file "
            "stem — case preserved."
        ),
    )
    parser.add_argument("--engine", default="pyspark", help="Compute engine (default: pyspark).")
    parser.add_argument(
        "--extra-mount",
        dest="extra_mounts",
        action="append",
        default=None,
        metavar="HOST:CONTAINER[:MODE]",
        help=(
            "Additional bind mount for the per-invocation container, "
            "e.g. /scratch/demo_out:/copy_out:rw. Repeatable. MODE is "
            "'rw' (default) or 'ro'. Container path must not shadow "
            "the standard mount set (/app, /data, /ovid2-user-config, "
            "/ovid_credentials, /usr/include/rsa). Only accepted by "
            "'ovid2 tools copy' — the ETL family rejects it."
        ),
    )
    parser.set_defaults(_entry=main)


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools copy``.

    Delegates to :func:`_run_host_mode` when invoked outside a
    container (spawns a per-invocation Docker cluster and re-invokes
    itself inside the container), or to :func:`_run_container_mode`
    when already inside the container.
    """
    # Local import so ``ovid2 --help`` stays fast.
    from ovid2.core.docker import is_inside_docker
    from ovid2.drivers.engine_compat import assert_engine_supports_drivers
    from ovid2.tools.extra_mounts import ExtraMountError, parse_extra_mounts
    from ovid2.tools.table_specs import (
        TableSpecError,
        is_all_sentinel,
        parse_table_specs,
    )

    # Validate table specs on both host and container so a malformed
    # entry aborts before we spin up a Spark cluster on the host, and
    # before we open any addresses on the container. The parsed list
    # is stashed on ``args`` so the container-mode loop can consume
    # it directly. The ``all`` sentinel is left unparsed: the source
    # driver enumerates its tables inside the container (where the
    # engine session and driver are live) — see ``_run_container_mode``.
    if is_all_sentinel(args.tables):
        args._resolved_tables = None
    else:
        try:
            args._resolved_tables = parse_table_specs(args.tables)
        except TableSpecError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Validate engine/driver family compatibility BEFORE any cluster
    # start-up. This closes the ``--engine snowpark`` + non-Snowflake
    # endpoint failure mode where the container used to fail at
    # ``from snowflake.snowpark import Session`` (missing wheel) or
    # would have failed later at ``s3a://...`` write time (Snowpark
    # DataFrames live inside Snowflake). Skipped when either address
    # cannot be resolved to a registered driver — the failure will
    # speak for itself downstream. Runs on both the host side (before
    # ``docker compose up``) and container side (before
    # ``engine.get_session()``) — the host-side call saves the
    # cluster-start cost; the container-side call is belt+suspenders
    # for direct-in-container invocations.
    from ovid2.core.loaders import load_engine

    try:
        engine = load_engine(args.engine)
        assert_engine_supports_drivers(
            engine,
            [args.from_address, args.to_address],
        )
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Validate --extra-mount specs up-front so a bad path fails before
    # we spin up a 25-worker Spark cluster. The parsed list is stashed
    # on ``args`` so downstream (docker.py / compose.py) don't have to
    # re-parse. Container mode doesn't need the parsed form — the
    # mounts are already applied by the host-side ``docker run`` /
    # ``docker compose up`` — so we only parse and stash on the host.
    if not is_inside_docker():
        try:
            explicit_mounts = parse_extra_mounts(getattr(args, "extra_mounts", None))
        except ExtraMountError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # Auto-mount every driver referenced by --from / --to via the
        # D5 collector — NOT the disc-only ``build_disc_mounts``. The
        # collector calls each driver's ``container_requirements_for``
        # so non-disc peripherals (Access .accdb, and any future driver
        # that declares a mount) contribute their bind mount too. An
        # explicit --extra-mount for the same container path wins; the
        # collector applies reserved-path filtering. (``move`` was
        # migrated to this path in D3; ``copy`` lagged behind — see
        # .opencode/knowledge/delta/plan-d3-native-drivers-and-move-script.md.)
        from ovid2.drivers.collector import collect_for_addresses

        collected = collect_for_addresses(
            [args.from_address, args.to_address],
            explicit_container_paths=[m.container for m in explicit_mounts],
        )
        auto_mounts = [_driver_mount_to_tool_mount(m) for m in collected.mounts]
        args.parsed_extra_mounts = [
            *explicit_mounts,
            *(m for m in auto_mounts if m is not None),
        ]

    if not is_inside_docker():
        return _run_host_mode(args)
    return _run_container_mode(args)


def _driver_mount_to_tool_mount(m: object) -> object | None:
    """Convert a :class:`drivers.container.ExtraMount` → :class:`tools.extra_mounts.ExtraMount`.

    The two dataclasses live in separate modules with different
    attribute names (``host_path`` / ``container_path`` vs
    ``host`` / ``container``) because they historically served
    different call sites. The docker + compose layer consumes the
    ``tools`` shape, so the collector's driver-side output has to
    be reshaped here. Kept identical to
    :func:`ovid2.tools.move._driver_mount_to_tool_mount` so the two
    one-shot tools reshape driver mounts the same way.

    Returns ``None`` when the driver mount fails validation (the
    ``tools`` grammar refuses non-existent host paths, relative
    paths, etc.) — the collector already applied reserved-path
    filtering, so failures here are exotic and non-fatal.
    """
    from ovid2.tools.extra_mounts import ExtraMountError, parse_extra_mount

    host = getattr(m, "host_path", None)
    container = getattr(m, "container_path", None)
    mode = getattr(m, "mode", "rw")
    if not host or not container:
        return None
    spec = f"{host}:{container}:{mode}"
    try:
        return parse_extra_mount(spec)
    except ExtraMountError as exc:
        logger.warning(
            "Driver auto-mount %r rejected by tools.extra_mounts: %s",
            spec,
            exc,
        )
        return None


def _run_host_mode(args: argparse.Namespace) -> int:
    """Dispatch to Docker (with or without an isolated Spark cluster).

    Mirrors :func:`ovid2.cli.etl._run_host_mode` — the isolation
    contract is identical: per-invocation compose file, network,
    signal directory, and event log; ``SPARK_MODE`` in settings picks
    between cluster and local Spark.
    """
    from ovid2.config.settings import SPARK_MODE
    from ovid2.core.docker import submit_to_docker, submit_with_cluster
    from ovid2.core.isolation import OvidInstance, cleanup_stale_instances

    cleanup_stale_instances()
    instance = OvidInstance(instance_id=os.environ.get("OVID2_INSTANCE_ID") or None)
    logger.info("Ovid2 tools-copy host mode: instance %s", instance.instance_id)

    # ``build_docker_command`` dispatches on ``args.command``. For
    # ``tools copy`` we need the two-token form so the ENTRYPOINT
    # receives ``ovid2 tools copy …`` — see
    # :func:`ovid2.core.docker._build_ovid2_argv`.
    args.command = "tools copy"

    # ``build_docker_command`` also reads ``args.input_address`` /
    # ``args.output_address`` on the etl path; the tools-copy path
    # accepts either those or ``from_address`` / ``to_address``. Alias
    # them so any future etl-shape codepath (e.g. Snowflake credential
    # forwarding, host-side arg logging) sees a consistent view.
    args.input_address = args.from_address
    args.output_address = args.to_address
    # These attributes exist on the etl parser but not on ours — set
    # empty defaults so ``build_docker_command``'s optional-flag loop
    # doesn't ``AttributeError`` if something reads them.
    args.transformations = []
    for attr in ("sf_database", "sf_account", "sf_schema"):
        if not hasattr(args, attr):
            setattr(args, attr, None)
    # ``submit_with_cluster`` reads ``args.workers`` to size the
    # per-invocation Spark cluster. The copy argparse deliberately
    # omits ``--workers`` (per the copy-kind ADR: workers is a
    # scrubbed pipeline-only field) so provide the same default the
    # etl parser uses. This only affects host→docker dispatch when
    # ``SPARK_MODE == "cluster"``.
    if not hasattr(args, "workers"):
        args.workers = 25

    if SPARK_MODE == "cluster":
        return submit_with_cluster(args, instance)
    return submit_to_docker(args, instance)


def _run_container_mode(args: argparse.Namespace) -> int:
    """Run the copy loop directly, inside the Spark container."""
    from ovid2.config.settings import WORK_DIR
    from ovid2.core.events import (
        instance_finished,
        instance_started,
    )
    from ovid2.core.isolation import OvidInstance
    from ovid2.core.loaders import load_engine
    from ovid2.display.bus import JsonlEventBus
    from ovid2.drivers.registry import driver_from_address

    instance = OvidInstance(instance_id=os.environ.get("OVID2_INSTANCE_ID") or None)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    bus = JsonlEventBus(WORK_DIR / f"events-{instance.instance_id}.jsonl")
    bus.emit(instance_started(instance_id=instance.instance_id, pipeline=args.pipeline))

    # Resolve both endpoints to native :class:`Driver` instances so
    # the engine's stager can bridge each into (or out of) the compute
    # session. The stager is what makes ``--engine`` orthogonal to the
    # storage backend: Spark-family stagers pass through; Snowpark's
    # stager dispatches on ``driver.type_name``.
    input_driver = driver_from_address(args.from_address, pipeline=args.pipeline)
    output_driver = driver_from_address(args.to_address, pipeline=args.pipeline)
    engine = load_engine(args.engine)
    stager = engine.get_stager()

    # Start the live-metrics sampler once the engine is up.
    from ovid2.core.metrics_sampler import run_sampler

    sampler = run_sampler(instance_id=instance.instance_id, engine=engine, bus=bus)

    # ``args._resolved_tables`` is populated by :func:`main` for an
    # explicit table list. When it is ``None`` the operator passed the
    # ``all`` sentinel (or invoked this function directly): expand it
    # against the source driver, which enumerates every user table in
    # the source (e.g. the tables inside an Access ``.accdb`` file).
    from ovid2.tools.table_specs import (
        TableSpec,
        is_all_sentinel,
        parse_table_specs,
    )

    resolved = getattr(args, "_resolved_tables", None)
    if resolved is None:
        if is_all_sentinel(args.tables):
            # Ensure a compute session is live before enumerating: the
            # container image ships the UCanAccess jars but not the
            # ``[access]`` extra (jaydebeapi), so table discovery falls
            # back to the Spark JDBC path, which needs an active session.
            # ``get_session`` is idempotent (the engine caches it) so
            # this does not double-start the cluster.
            engine.get_session()
            expanded = input_driver.expand_all_tables()
            if expanded is None:
                raise ValueError(
                    f"source '{args.from_address}' "
                    f"({input_driver.type_name}) cannot expand `--tables "
                    f"all`; pass an explicit table list."
                )
            resolved = [TableSpec(table_name=name, file_name=name) for name in expanded]
            print(
                f"[ovid2 tools copy] expanded --tables all → "
                f"{len(resolved)} table(s): {', '.join(t.table_name for t in resolved)}"
            )
        else:
            resolved = parse_table_specs(args.tables)

    failures = 0
    try:
        failures = _copy_tables_concurrently(
            resolved,
            stager,
            input_driver,
            output_driver,
            args,
            bus=bus,
            instance_id=instance.instance_id,
        )
    finally:
        sampler.stop()
        engine.stop_session()
    bus.emit(instance_finished(instance_id=instance.instance_id, success=failures == 0))
    bus.close()
    return 0 if failures == 0 else 1


def _copy_tables_concurrently(
    resolved: list[TableSpec],
    stager: EngineStager,
    input_driver: Driver,
    output_driver: Driver,
    args: argparse.Namespace,
    *,
    bus: EventBus | None = None,
    instance_id: str = "",
) -> int:
    """Copy every resolved table, fanning out across driver threads.

    Each table's ``materialise`` (lazy) + ``persist`` (the Spark action
    that triggers the write) runs on its own worker thread. Because a
    single Access/disc table read is one non-splittable partition, one
    table occupies exactly one executor; submitting several tables at
    once from independent threads lets Spark's scheduler run those
    single-task jobs in parallel across the whole cluster instead of
    leaving 24 of 25 workers idle. Spark supports concurrent job
    submission from multiple threads on one driver, and the Spark-family
    stager is a stateless pass-through, so this is safe.

    When a ``bus`` is supplied, each table emits ``table.started`` →
    ``table.completed`` / ``table.failed`` so the live run console shows
    the per-table roster fill in — the same event stream every other
    pipeline type renders (the copy has no CDM roster to pre-seed, so
    these events are how the run page discovers the tables). The bus is
    thread-safe (``JsonlEventBus`` guards its writes with a lock), so it
    is safe to emit from the worker threads.

    Returns the number of tables that failed to copy.
    """
    import time

    from ovid2.core.events import table_completed, table_failed, table_started

    concurrency = min(_copy_table_concurrency(), len(resolved)) or 1

    def _emit(event_factory: object, **kwargs: object) -> None:
        """Emit an event on the bus when one is configured (no-op otherwise)."""
        if bus is None:
            return
        bus.emit(event_factory(instance_id=instance_id, **kwargs))  # type: ignore[operator]

    def _copy_one(spec: TableSpec) -> tuple[str, str | None]:
        """Copy one table; return ``(table_name, error_or_None)``."""
        table_name = spec.table_name
        file_name = spec.file_name
        started = time.monotonic()
        _emit(
            table_started,
            pipeline=args.pipeline,
            batch_id=args.batch_id,
            table=table_name,
        )
        try:
            df = stager.materialise(
                input_driver,
                args.pipeline,
                args.batch_id,
                file_name,
                table_name=table_name,
            )
            stager.persist(
                df,
                output_driver,
                args.pipeline,
                args.batch_id,
                table_name,
            )
            _emit(
                table_completed,
                pipeline=args.pipeline,
                batch_id=args.batch_id,
                table=table_name,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
            return (table_name, None)
        except Exception as exc:  # copy is best-effort per table
            logger.exception("copy failed for %s", table_name)
            _emit(
                table_failed,
                pipeline=args.pipeline,
                batch_id=args.batch_id,
                table=table_name,
                error=str(exc),
            )
            return (table_name, str(exc))

    # A single table (or concurrency==1) keeps the original serial,
    # thread-free behaviour — no pool overhead, easier tracebacks.
    if concurrency == 1:
        failures = 0
        for spec in resolved:
            _table_name, error = _copy_one(spec)
            if error is None:
                print(f"[ovid2 tools copy] {_table_name}: OK")
            else:
                failures += 1
                print(
                    f"[ovid2 tools copy] {_table_name}: FAILED — {error}",
                    file=sys.stderr,
                )
        return failures

    print(f"[ovid2 tools copy] copying {len(resolved)} table(s) with concurrency {concurrency}")
    failures = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_copy_one, spec): spec for spec in resolved}
        for future in as_completed(futures):
            table_name, error = future.result()
            if error is None:
                print(f"[ovid2 tools copy] {table_name}: OK")
            else:
                failures += 1
                print(
                    f"[ovid2 tools copy] {table_name}: FAILED — {error}",
                    file=sys.stderr,
                )
    return failures
