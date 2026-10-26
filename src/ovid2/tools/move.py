"""``ovid2 tools move`` — driver-native one-shot data move.

Successor to :mod:`ovid2.tools.copy`. Same CLI surface, same isolation
contract, same event schema — but the data plane routes through the
D3 driver stack (:mod:`ovid2.drivers.registry`) instead of the legacy
:class:`~ovid2.core.addresses.base.BaseAddress` machinery.

Concretely, three things differ from ``tools copy``:

1. **Driver-based reads/writes.** Each ``--from`` / ``--to`` address
   is resolved to a native :class:`~ovid2.drivers.base.Driver` via
   :func:`ovid2.drivers.registry.driver_from_address`. The driver's
   ``.read()`` / ``.write()`` methods are called directly; the
   legacy ``BaseAddress`` shim is skipped.

2. **Container-requirement collection.** Auto-mounts come from
   :func:`ovid2.drivers.collector.collect_for_addresses` (D5) instead
   of :func:`ovid2.connections.disc_mounts.build_disc_mounts`. Same
   observable mount set for disc addresses today; the collector's
   uniform interface lets future drivers contribute Java packages
   and env overlays without a caller-side change.

3. **Advisory type-stamp check.** Before launching the container-side
   loop we call
   :func:`ovid2.drivers.type_stamping.validate_route_types` (D6.0)
   with ``strict=False`` by default. When ``--strict-types`` is
   passed, a source/target CDM mismatch aborts the run instead of
   logging a warning.

``ovid2 tools copy`` continues to work unchanged — this command is
additive so pipelines can adopt it at their own pace.

Usage::

    ovid2 tools move \\
        --from demo_disc --to demo_snowflake \\
        --pipeline demo --batch_id 20260316 \\
        --tables DEMOGRAPHIC ENCOUNTER DIAGNOSIS \\
        [--engine pyspark] \\
        [--extra-mount /host/path:/container/path[:rw|ro]] ... \\
        [--strict-types]

Each ``--tables`` entry is either a bare logical table name
(``HASH_TOKEN``) or a ``LOGICAL=SOURCE`` split
(``HASH_TOKEN=*datavant_person_tokens*``). The LOGICAL half is always
used as the destination file stem (case preserved verbatim); the
SOURCE half, when present, is the source-side lookup key handed to
the driver's ``read()``. This codifies the "mirror-the-logical-name"
rule: an ACME-shaped run of
``--tables 'HASH_TOKEN=*datavant_person_tokens*'`` reads the disc
glob but writes ``HASH_TOKEN.csv`` on the destination — no glob
metacharacters end up in the output key. See
:mod:`ovid2.tools.table_specs` for the full grammar.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tools move`` subcommand."""
    parser = subparsers.add_parser(
        "move",
        help=(
            "Move tables from one address to another (driver-native; "
            "D3 successor to 'ovid2 tools copy')."
        ),
        description=__doc__,
    )
    parser.add_argument(
        "--from",
        dest="from_address",
        required=True,
        help="Name of the source address config.",
    )
    parser.add_argument(
        "--to",
        dest="to_address",
        required=True,
        help="Name of the destination address config.",
    )
    parser.add_argument("--pipeline", required=True, help="Pipeline name.")
    parser.add_argument(
        "--batch_id",
        required=True,
        help="Batch id — the complete Data Space path of the input location (<connection>/<path>); a run label, never appended to an address.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Allow writing into an output location that already holds tables "
            "(the address is the exact write location; without this flag the "
            "run refuses to overwrite existing data)."
        ),
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        required=True,
        help=(
            "Table specs to move. Each entry is either 'LOGICAL' "
            "(source basename equals logical name) or "
            "'LOGICAL=SOURCE' (LOGICAL is the destination file stem; "
            "SOURCE is the source-side lookup key, e.g. a disc glob "
            "'*datavant_person_tokens*' or a Snowflake dbtable). "
            "LOGICAL is always used verbatim as the destination file "
            "stem — case preserved."
        ),
    )
    parser.add_argument(
        "--engine",
        default="pyspark",
        help="Compute engine (default: pyspark).",
    )
    parser.add_argument(
        "--extra-mount",
        dest="extra_mounts",
        action="append",
        default=None,
        metavar="HOST:CONTAINER[:MODE]",
        help=(
            "Additional bind mount for the per-invocation container. "
            "Repeatable. Same grammar and reserved-path rules as "
            "'ovid2 tools copy'; see ovid2.tools.extra_mounts."
        ),
    )
    parser.add_argument(
        "--strict-types",
        dest="strict_types",
        action="store_true",
        default=False,
        help=(
            "Fail the run when source and target addresses declare "
            "different CDMs. Default is advisory-only (a warning is "
            "logged; the run continues). Matches the D6.0 route-"
            "validation contract."
        ),
    )
    parser.set_defaults(_entry=main)


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools move``.

    Host mode spawns a Docker cluster and re-invokes itself inside
    the container; container mode does the actual read/write loop.
    """
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
    # it directly. Argv is preserved verbatim across the host→
    # container hop by :func:`ovid2.core.docker._build_ovid2_argv_move`,
    # so the split ``LOGICAL=SOURCE`` form round-trips unchanged. The
    # ``all`` sentinel is left unparsed and expanded in the container
    # against the source driver (see ``_run_container_mode``).
    if is_all_sentinel(args.tables):
        args._resolved_tables = None
    else:
        try:
            args._resolved_tables = parse_table_specs(args.tables)
        except TableSpecError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Refuse an incompatible engine/driver pairing before any Spark
    # cluster spins up. Snowpark only works with Snowflake endpoints;
    # every other driver defaults to ``{"spark"}``. See
    # :mod:`ovid2.drivers.engine_compat` for the full rationale.
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

    # Non-Spark engines (Snowpark) compute inside the warehouse — the
    # host side then runs a single driver container, no Spark cluster.
    from ovid2.core.docker import engine_needs_spark_cluster

    args._spark_cluster = engine_needs_spark_cluster(engine)

    if not is_inside_docker():
        # Validate operator-supplied mounts up-front so a bad spec
        # fails before we spin up a Spark cluster.
        try:
            explicit_mounts = parse_extra_mounts(getattr(args, "extra_mounts", None))
        except ExtraMountError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # D5 collector produces mounts / java packages / env overlays
        # for every driver referenced by the run. Today, only disc
        # actually contributes an ExtraMount, so the output is
        # observationally equivalent to ``build_disc_mounts``. Java
        # packages + env overlays are ignored on the host side —
        # they surface via the driver's own ``spark.jars.packages``
        # / env-injection paths (E2, follow-up).
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
        return _run_host_mode(args)
    return _run_container_mode(args)


def _driver_mount_to_tool_mount(m: object) -> object | None:
    """Convert a :class:`drivers.container.ExtraMount` → :class:`tools.extra_mounts.ExtraMount`.

    The two dataclasses live in separate modules with different
    attribute names (``host_path`` / ``container_path`` vs
    ``host`` / ``container``) because they historically served
    different call sites. The docker + compose layer consumes the
    ``tools`` shape, so the collector's driver-side output has to
    be reshaped here.

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

    Mirrors :func:`ovid2.tools.copy._run_host_mode` — the isolation
    contract is identical.
    """
    from ovid2.config.settings import SPARK_MODE
    from ovid2.core.docker import submit_one_shot
    from ovid2.core.isolation import OvidInstance, cleanup_stale_instances

    cleanup_stale_instances()
    instance = OvidInstance(
        instance_id=os.environ.get("OVID2_INSTANCE_ID") or None,
    )
    logger.info("Ovid2 tools-move host mode: instance %s", instance.instance_id)

    # ``build_docker_command`` dispatches on ``args.command``. The
    # ``tools move`` value is recognised alongside ``tools copy`` in
    # ``ovid2.core.docker`` — see :func:`_build_ovid2_argv`.
    args.command = "tools move"
    args.input_address = args.from_address
    args.output_address = args.to_address
    args.transformations = []
    for attr in ("sf_database", "sf_account", "sf_schema"):
        if not hasattr(args, attr):
            setattr(args, attr, None)
    if not hasattr(args, "workers"):
        args.workers = 25

    return submit_one_shot(
        args,
        instance,
        spark_cluster=bool(getattr(args, "_spark_cluster", True)),
        spark_mode=SPARK_MODE,
    )


def _run_container_mode(args: argparse.Namespace) -> int:
    """Run the move loop directly, inside the Spark container.

    The container-side flow:

    1. Advisory type-stamp check (:mod:`drivers.type_stamping`).
       Logs a warning when source/target disagree on CDM; raises
       when ``--strict-types`` is passed.
    2. Resolve ``--from`` / ``--to`` to native :class:`Driver`
       instances via :func:`drivers.registry.driver_from_address`.
    3. Load the compute engine and its stager
       (:meth:`BaseEngine.get_stager`). The stager is the
       engine-scoped bridge between an :class:`EngineSession` and a
       :class:`Driver`; Spark-family stagers pass through
       (unchanged), Snowpark's stager dispatches on
       ``driver.type_name``.
    4. For each table: ``stager.materialise()`` →
       ``stager.persist()``. Emit
       ``table_started`` / ``table_completed`` / ``table_failed``
       events on the JSONL bus. Failures are counted but do not
       abort the run (the copy-family contract).
    5. Emit ``instance_finished`` with an aggregate success flag.
    """
    from ovid2.config.settings import WORK_DIR
    from ovid2.core.events import (
        instance_finished,
        instance_started,
        table_completed,
        table_failed,
        table_started,
    )
    from ovid2.core.isolation import OvidInstance
    from ovid2.core.loaders import load_engine
    from ovid2.display.bus import JsonlEventBus
    from ovid2.drivers.registry import driver_from_address
    from ovid2.drivers.type_stamping import validate_route_types

    instance = OvidInstance(
        instance_id=os.environ.get("OVID2_INSTANCE_ID") or None,
    )

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    bus = JsonlEventBus(WORK_DIR / f"events-{instance.instance_id}.jsonl")
    bus.emit(
        instance_started(instance_id=instance.instance_id, pipeline=args.pipeline),
    )

    # Advisory type check (D6.0). Passes ``strict`` through so a
    # mismatch raises when the operator opts in.
    try:
        validate_route_types(
            route_name="tools_move",
            from_address=args.from_address,
            to_address=args.to_address,
            strict=bool(getattr(args, "strict_types", False)),
        )
    except Exception as exc:
        # Under --strict-types this is a hard failure; log + bail
        # with a clean exit code.
        print(
            f"[ovid2 tools move] strict-type check failed: {exc}",
            file=sys.stderr,
        )
        bus.emit(
            instance_finished(instance_id=instance.instance_id, success=False),
        )
        bus.close()
        return 1

    try:
        src_driver = driver_from_address(
            args.from_address,
            pipeline=args.pipeline,
        )
        dst_driver = driver_from_address(
            args.to_address,
            pipeline=args.pipeline,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(
            f"[ovid2 tools move] address resolution failed: {exc}",
            file=sys.stderr,
        )
        bus.emit(
            instance_finished(instance_id=instance.instance_id, success=False),
        )
        bus.close()
        return 2

    engine = load_engine(args.engine)
    # Route through the engine's stager so ``--engine`` is orthogonal
    # to the storage backend on either end. Spark-family stagers pass
    # through to ``driver.read`` / ``driver.write`` verbatim (zero
    # behaviour change); the Snowpark stager dispatches on
    # ``driver.type_name``.
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
            # ``get_session`` is idempotent (the engine caches it).
            engine.get_session()
            expanded = src_driver.expand_all_tables(
                pipeline=args.pipeline,
                batch_id=args.batch_id,
            )
            if expanded is None:
                raise ValueError(
                    f"source '{args.from_address}' "
                    f"({src_driver.type_name}) cannot expand `--tables "
                    f"all`; pass an explicit table list."
                )
            resolved = parse_table_specs(expanded)
            print(
                f"[ovid2 tools move] expanded --tables all → "
                f"{len(resolved)} table(s): {', '.join(t.table_name for t in resolved)}"
            )
        else:
            resolved = parse_table_specs(args.tables)

    # Largest-first order (parity with ``tools copy``). ``move`` runs
    # tables serially, so this does not shorten the run; it makes the
    # big tables fail fast and keeps the two tools' rosters consistent.
    from ovid2.tools.table_order import order_largest_first

    resolved, _sizes = order_largest_first(
        resolved,
        src_driver,
        pipeline=args.pipeline,
        batch_id=args.batch_id,
    )

    # Overwrite gate: the output address is the exact write location. A
    # table this run would write that already exists there needs an
    # explicit ``--overwrite``.
    from ovid2.drivers.overwrite import OutputNotEmptyError, assert_output_writable

    try:
        assert_output_writable(
            args.to_address,
            overwrite=bool(getattr(args, "overwrite", False)),
            driver=dst_driver,
            only_tables=[t.table_name for t in resolved],
        )
    except OutputNotEmptyError as exc:
        print(f"[ovid2 tools move] ERROR {exc}", file=sys.stderr)
        sampler.stop()
        engine.stop_session()
        bus.emit(instance_finished(instance_id=instance.instance_id, success=False))
        bus.close()
        return 3

    failures = 0
    try:
        for spec in resolved:
            table_name = spec.table_name
            file_name = spec.file_name
            bus.emit(
                table_started(
                    instance_id=instance.instance_id,
                    pipeline=args.pipeline,
                    batch_id=args.batch_id,
                    table=table_name,
                ),
            )
            try:
                df = stager.materialise(
                    src_driver,
                    args.pipeline,
                    args.batch_id,
                    file_name,
                    table_name=table_name,
                )
                stager.persist(
                    df,
                    dst_driver,
                    args.pipeline,
                    args.batch_id,
                    table_name,
                )
                bus.emit(
                    table_completed(
                        instance_id=instance.instance_id,
                        pipeline=args.pipeline,
                        batch_id=args.batch_id,
                        table=table_name,
                    ),
                )
                print(f"[ovid2 tools move] {table_name}: OK")
            except Exception as exc:
                failures += 1
                logger.exception("move failed for %s", table_name)
                bus.emit(
                    table_failed(
                        instance_id=instance.instance_id,
                        pipeline=args.pipeline,
                        batch_id=args.batch_id,
                        table=table_name,
                        error=str(exc),
                    ),
                )
                print(
                    f"[ovid2 tools move] {table_name}: FAILED — {exc}",
                    file=sys.stderr,
                )
    finally:
        sampler.stop()
        engine.stop_session()
    bus.emit(
        instance_finished(instance_id=instance.instance_id, success=failures == 0),
    )
    bus.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.tools.move is a CLI subcommand; invoke via 'ovid2 tools move …'.")
