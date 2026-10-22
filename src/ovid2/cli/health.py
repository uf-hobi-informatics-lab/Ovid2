"""``ovid2 health`` — data-health / PCORnet EDC checks.

Points at an input source (a database or storage account where CDM tables
already live), runs the declarative data-health checks selected from
:mod:`ovid2.transformations.quality.registry`, writes a durable metric-row
``dcq_results`` table to the output storage account, and drops a compact
JSON report artifact beside the event log for the web result viewer.

Unlike ``ovid2 etl`` this is a cross-table analysis pass, so it does not
run the format→dedup→map→fix chain — it reads the CDM tables as they are.

Two modes (mirrors ``ovid2 etl``):

- **Host mode** (outside Docker): builds a ``docker run`` and optionally
  starts / tears down an isolated Spark cluster.
- **Container mode** (inside Docker): runs the checks directly against the
  engine session.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.core.addresses.base import BaseAddress

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``health`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "health",
        help="Run data-health / PCORnet EDC checks and produce a report.",
        description=(
            "Run the PCORnet EDC / Data Curation Query checks plus Ovid2 "
            "health panels against an input source, writing a durable "
            "results table and a navigable JSON report."
        ),
    )
    parser.set_defaults(_command="health", _entry=main)

    parser.add_argument("--input_address", help="Input source address name.")
    parser.add_argument("--output_address", help="Where to write the results table.")
    parser.add_argument(
        "--input_connection",
        default=None,
        help="Storage account (connection) name for the input, used with "
        "--input_location to select the database/schema at run time instead "
        "of a saved --input_address. Lets one reusable EDC run target any "
        "database of the CDM.",
    )
    parser.add_argument(
        "--input_location",
        default=None,
        help="Browsed/typed location for --input_connection, e.g. "
        "'DATABASE/SCHEMA' (Snowflake) or a folder path.",
    )
    parser.add_argument(
        "--output_connection",
        default=None,
        help="Storage account (connection) name for the output results table, "
        "used with --output_location instead of a saved --output_address.",
    )
    parser.add_argument(
        "--output_location",
        default=None,
        help="Browsed/typed location for --output_connection, e.g. "
        "'DATABASE/SCHEMA' (Snowflake) or a folder path.",
    )
    parser.add_argument(
        "--pipeline",
        default="pcornet_edc",
        help="Pipeline label. Defaults to the generic 'pcornet_edc' EDC "
        "label (CDM pcornet_7_0) so a single reusable run can target any "
        "PCORnet database; pass a partner name only if you need its CDM/"
        "default-database behaviour.",
    )
    parser.add_argument("--batch_id", required=True, help="Batch / refresh identifier.")
    parser.add_argument(
        "--parts",
        nargs="+",
        default=["all"],
        help="Check parts to run (part1..part4, mapping, freshness, integrity, "
        "completeness, semantic) or 'all'.",
    )
    parser.add_argument(
        "--packages",
        nargs="+",
        default=None,
        help="EDC/DC report-cycle packages to run (e.g. 20r1). Runs the union "
        "of the packages' member checks. Overrides --parts; ignored when "
        "--checks is given.",
    )
    parser.add_argument(
        "--checks",
        nargs="+",
        default=None,
        help="Explicit check ids/names to run (e.g. 1.01 DC_3_09). Overrides "
        "--packages and --parts.",
    )
    parser.add_argument(
        "--list-packages",
        action="store_true",
        help="List the available EDC/DC report-cycle packages and exit.",
    )
    parser.add_argument(
        "--prev_input_address",
        default=None,
        help="Prior-refresh input address for the freshness / volume-trend panel.",
    )
    parser.add_argument("--engine", default="pyspark", help="Compute engine (default: pyspark).")
    parser.add_argument("--workers", type=int, default=25, help="Workers for an isolated cluster.")
    parser.add_argument("--sf_database", default=None)
    parser.add_argument("--sf_account", default=None)
    parser.add_argument("--sf_schema", default=None)
    parser.add_argument("--cdm", default=None, help="CDM name (default: pipeline's CDM).")
    parser.add_argument(
        "--stderr-log",
        action="store_true",
        help="Mirror every event to stderr as a plain-text log line.",
    )


def _list_packages() -> int:
    """Print the available EDC/DC report-cycle packages and return 0."""
    from ovid2.transformations.quality.registry import load_all_packages

    pkgs = load_all_packages()
    if not pkgs:
        print("[ovid2] health: no check packages registered.")
        return 0
    print("[ovid2] Available EDC/DC report-cycle packages (most recent first):")
    for pkg in pkgs:
        print(f"  {pkg.id:<10} {pkg.released}  {pkg.label} ({len(pkg.checks)} checks)")
    return 0


def _sf_overrides(args: argparse.Namespace) -> dict[str, str] | None:
    overrides: dict[str, str] = {}
    if args.sf_database:
        overrides["database"] = args.sf_database
    if args.sf_account:
        overrides["account"] = args.sf_account
    if args.sf_schema:
        overrides["schema"] = args.sf_schema
    return overrides or None


def _location_from_str(location: str | None) -> dict[str, str]:
    """Parse a ``--*_location`` string into a location dict.

    Accepts a warehouse ``DATABASE/SCHEMA`` (or bare ``DATABASE``) form
    as well as an arbitrary object-store ``path``. Both are stored under
    ``path`` and split downstream by the driver's ``build_address_config``
    (Snowflake splits ``database/schema``; object stores keep the path).
    """
    folder = (location or "").strip().strip("/")
    return {"path": folder} if folder else {}


def _load_endpoint(
    address_name: str | None,
    connection_name: str | None,
    location: str | None,
    *,
    sf_overrides: dict[str, str] | None,
    pipeline: str,
    cdm_name: str,
    role: str,
) -> BaseAddress:
    """Resolve one health endpoint from either a saved address or a
    run-time-selected storage account + location.

    ``connection`` + ``location`` win when supplied (run-time selection);
    otherwise the saved ``address`` name is loaded. Raises ``ValueError``
    when neither is given.
    """
    from ovid2.config.addresses import load_address_from_connection
    from ovid2.core.loaders import load_address

    if connection_name:
        return load_address_from_connection(
            connection_name,
            _location_from_str(location),
            sf_overrides=sf_overrides,
            pipeline=pipeline,
            cdm_name=cdm_name,
        )
    if address_name:
        return load_address(
            address_name, sf_overrides=sf_overrides, pipeline=pipeline, cdm_name=cdm_name
        )
    raise ValueError(
        f"health: no {role} endpoint — pass --{role}_address or "
        f"--{role}_connection (+ --{role}_location)."
    )


def main(args: argparse.Namespace) -> int:
    """Entry point invoked by :mod:`ovid2.cli.__main__`."""
    from ovid2.core.docker import is_inside_docker

    args.command = "health"

    if getattr(args, "list_packages", False):
        return _list_packages()

    # Engine/driver compatibility gate (same as ovid2 etl): refuse
    # --engine snowpark paired with incompatible peripherals before a
    # cluster spins up.
    try:
        from ovid2.drivers.engine_compat import assert_engine_type_supports_drivers

        # Only saved-address names can be pre-checked here; run-time
        # connection selections (--input_connection / --output_connection)
        # are resolved later in container mode. Filter out the empty ones.
        addresses = [
            a
            for a in (
                getattr(args, "input_address", None),
                getattr(args, "output_address", None),
                getattr(args, "prev_input_address", None),
            )
            if a
        ]
        if addresses:
            assert_engine_type_supports_drivers(args.engine, addresses)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("engine_compat check skipped: %s", exc)

    if not is_inside_docker():
        return _run_host_mode(args)
    return _run_container_mode(args)


def _run_host_mode(args: argparse.Namespace) -> int:
    """Dispatch the run to Docker (with or without an isolated cluster)."""
    from ovid2.config.settings import SPARK_MODE
    from ovid2.connections.disc_mounts import build_disc_mounts
    from ovid2.core.docker import submit_to_docker, submit_with_cluster
    from ovid2.core.isolation import OvidInstance, cleanup_stale_instances

    cleanup_stale_instances()
    instance = OvidInstance()
    logger.info("Ovid2 health host mode: instance %s", instance.instance_id)

    # Preserve the caller's command token (``dq_checks`` reuses this runner
    # but must keep its own subcommand token so the container argv and the
    # read-only ``/data`` mount stay correct); default to ``health``.
    args.command = getattr(args, "command", None) or "health"
    args.parsed_extra_mounts = build_disc_mounts(
        [
            name
            for name in (
                getattr(args, "input_address", None),
                getattr(args, "output_address", None),
                getattr(args, "prev_input_address", None),
            )
            if name
        ],
    )

    if SPARK_MODE == "cluster":
        return submit_with_cluster(args, instance)
    return submit_to_docker(args, instance)


def _run_container_mode(args: argparse.Namespace) -> int:
    """Run the health checks directly, inside the Spark container."""
    from ovid2.config.settings import WORK_DIR
    from ovid2.core.events import (
        EventBus,
        instance_finished,
        instance_started,
        set_default_bus,
    )
    from ovid2.core.isolation import OvidInstance
    from ovid2.core.loaders import load_address, load_engine
    from ovid2.display.bus import JsonlEventBus, TeeEventBus
    from ovid2.display.renderers.plain_log import PlainLogBus
    from ovid2.transformations.quality import report as report_mod
    from ovid2.transformations.quality.registry import parts as all_parts
    from ovid2.transformations.quality.runner import (
        HealthCheckRunner,
        all_candidate_tables,
        select_health_specs,
    )

    pipeline = args.pipeline
    batch_id = args.batch_id

    parts_arg = args.parts
    if parts_arg == ["all"] or parts_arg == "all":
        parts_arg = None
    specs = select_health_specs(parts_arg, args.checks, getattr(args, "packages", None))
    if not specs:
        print("[ovid2] health: no checks selected — nothing to do.", file=sys.stderr)
        return 1

    instance_id = os.environ.get("OVID2_INSTANCE_ID", "") or OvidInstance().instance_id
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    event_log = WORK_DIR / f"events-{instance_id}.jsonl"
    jsonl_bus = JsonlEventBus(event_log)
    bus: EventBus = TeeEventBus(jsonl_bus, PlainLogBus()) if args.stderr_log else jsonl_bus
    set_default_bus(bus)

    bus.emit(instance_started(instance_id=instance_id, pipeline=pipeline))

    engine = load_engine(args.engine)
    # The Spark-typed legacy ``BaseAddress.write`` takes this session;
    # ``cli/`` may not import pyspark (even for typing).
    session: Any = engine.get_session()
    engine.prepare_workspace()

    sf_overrides = _sf_overrides(args)
    cdm_name = getattr(args, "cdm", None)
    if not cdm_name:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        cdm_name = resolve_cdm_for_pipeline(pipeline).name
    from ovid2.cdm.registry import get_cdm

    cdm = get_cdm(cdm_name)

    input_address = _load_endpoint(
        getattr(args, "input_address", None),
        getattr(args, "input_connection", None),
        getattr(args, "input_location", None),
        sf_overrides=sf_overrides,
        pipeline=pipeline,
        cdm_name=cdm_name,
        role="input",
    )
    output_address = _load_endpoint(
        getattr(args, "output_address", None),
        getattr(args, "output_connection", None),
        getattr(args, "output_location", None),
        sf_overrides=sf_overrides,
        pipeline=pipeline,
        cdm_name=cdm_name,
        role="output",
    )

    print("[ovid2] Health run")
    print(f"[ovid2]   pipeline={pipeline} batch_id={batch_id}")
    print(f"[ovid2]   checks={[s.id for s in specs]}")
    print(f"[ovid2]   engine={args.engine} instance_id={instance_id}")
    print(f"[ovid2]   event_log={event_log}")

    def _endpoint_desc(address: str | None, conn: str | None, loc: str | None) -> str:
        if conn:
            return f"{conn}:{(loc or '').strip('/')}" if loc else conn
        return address or "(unknown)"

    input_desc = _endpoint_desc(
        getattr(args, "input_address", None),
        getattr(args, "input_connection", None),
        getattr(args, "input_location", None),
    )
    output_desc = _endpoint_desc(
        getattr(args, "output_address", None),
        getattr(args, "output_connection", None),
        getattr(args, "output_location", None),
    )

    runner = HealthCheckRunner(
        session=session,
        cdm=cdm,
        pipeline=pipeline,
        batch_id=batch_id,
        instance_id=instance_id,
        event_bus=bus,
    )

    failures = 0
    try:
        candidates = all_candidate_tables(specs)
        available = runner.register_input_tables(input_address, candidates)

        prior_tables: dict[str, object] = {}
        prev_name = getattr(args, "prev_input_address", None)
        if prev_name:
            prev_address = load_address(
                prev_name, sf_overrides=sf_overrides, pipeline=pipeline, cdm_name=cdm_name
            )
            prior_tables = runner.register_prior_tables(prev_address, candidates)

        outcomes = runner.run(specs, available_tables=available, prior_tables=prior_tables)
        failures = sum(1 for o in outcomes if o.status == "error")

        # Durable results table -> output storage account.
        results_uri: str | None = None
        try:
            results_df = runner.build_results_dataframe(outcomes)
            output_address.write(results_df, session, pipeline, batch_id, "dcq_results")
            results_uri = f"{output_desc}:{pipeline}/{batch_id}/dcq_results"
        except Exception as exc:
            logger.exception("health: results table write failed: %s", exc)
            print(f"[ovid2] WARNING results write failed: {exc}", file=sys.stderr)

        # JSON report artifact -> work dir (web viewer reads this).
        report = report_mod.build_report(
            instance_id=instance_id,
            pipeline=pipeline,
            batch_id=batch_id,
            cdm=cdm_name,
            input_address=input_desc,
            output_address=output_desc,
            outcomes=outcomes,
            results_uri=results_uri,
        )
        report_mod.write_report(report_mod.report_path(WORK_DIR, instance_id), report)

        summary = report["summary"]
        print(
            f"[ovid2] Health complete. checks={summary['checks_total']} "
            f"passed={summary['checks_passed']} exception={summary['checks_exception']} "
            f"error={summary['checks_error']}"
        )
    except Exception as exc:
        logger.exception("health run failed: %s", exc)
        print(f"[ovid2] ERROR health run: {exc}", file=sys.stderr)
        failures += 1
    finally:
        engine.stop_session()

    bus.emit(instance_finished(instance_id=instance_id, success=failures == 0))
    jsonl_bus.close()
    _ = all_parts  # referenced for potential future --list; silences linter
    return 0 if failures == 0 else 1
