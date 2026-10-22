"""``ovid2 dq_checks`` — data-quality / EDC report-cycle checks.

``dq_checks`` runs the PCORnet EDC / Data-Characterization checks selected
by *report cycle package* (e.g. ``20r1``, ``20r2``) against an input source
whose CDM tables already exist. It is the cycle-package-driven face of the
data-health engine: structurally it reuses the very same
:mod:`ovid2.transformations.quality` runner that ``ovid2 health`` uses
(reads CDM tables directly — no format→dedup→map→fix chain, no installed
``ovid2_pipelines.<name>`` package required), but it is surfaced as a
first-class pipeline kind whose "type" is the report cycle.

Selection precedence (highest first): ``--checks`` > ``--packages`` >
``--parts``. When nothing is selected the latest available cycle package is
used, so a bare ``ovid2 dq_checks`` run is still meaningful.

Two modes (mirrors ``ovid2 etl`` / ``ovid2 health``):

- **Host mode** (outside Docker): builds a ``docker run`` and optionally
  starts / tears down an isolated Spark cluster.
- **Container mode** (inside Docker): runs the checks directly against the
  engine session.
"""

from __future__ import annotations

import argparse

from ovid2.cli.health import _run_container_mode, _run_host_mode


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``dq_checks`` subcommand."""
    parser = subparsers.add_parser(
        "dq_checks",
        help="Run EDC/DC data-quality checks by report cycle (e.g. 20r1).",
        description=(
            "Run the PCORnet EDC / Data-Characterization checks selected by "
            "report-cycle package against an input source, writing a durable "
            "results table and a navigable JSON report. Reads CDM tables "
            "as-is (no format/map/fix chain, no pipeline package required)."
        ),
    )
    parser.set_defaults(_command="dq_checks", _entry=_entry)

    parser.add_argument("--input_address", help="Input source address name.")
    parser.add_argument("--output_address", help="Where to write the results table.")
    parser.add_argument(
        "--input_connection",
        default=None,
        help="Storage account (connection) for the input, used with "
        "--input_location to pick the database/schema at run time instead of "
        "a saved --input_address.",
    )
    parser.add_argument(
        "--input_location",
        default=None,
        help="Location for --input_connection, e.g. 'DATABASE/SCHEMA'.",
    )
    parser.add_argument(
        "--output_connection",
        default=None,
        help="Storage account (connection) for the results output.",
    )
    parser.add_argument(
        "--output_location",
        default=None,
        help="Location for --output_connection, e.g. 'DATABASE/SCHEMA'.",
    )
    parser.add_argument(
        "--pipeline",
        default="pcornet_edc",
        help="Pipeline label. Defaults to the generic 'pcornet_edc' EDC label "
        "(CDM pcornet_7_0) so one reusable run can target any PCORnet database.",
    )
    parser.add_argument("--batch_id", required=True)
    parser.add_argument(
        "--packages",
        nargs="+",
        default=None,
        help="EDC/DC report-cycle packages to run (e.g. 20r1). Runs the union "
        "of the packages' member checks. Overrides --parts; ignored when "
        "--checks is given. Defaults to the latest available cycle.",
    )
    parser.add_argument(
        "--parts",
        nargs="+",
        default=["all"],
        help="Check parts to run (part1..part4, mapping, freshness, integrity, "
        "completeness, semantic) or 'all'. Overridden by --packages / --checks.",
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
    parser.add_argument("--engine", default="pyspark")
    parser.add_argument("--workers", type=int, default=25)
    parser.add_argument("--sf_database", default=None)
    parser.add_argument("--sf_account", default=None)
    parser.add_argument("--sf_schema", default=None)
    parser.add_argument("--cdm", default=None, help="CDM name (default: pipeline's CDM).")
    parser.add_argument("--stderr-log", action="store_true")


def _entry(args: argparse.Namespace) -> int:
    """Entry point invoked by :mod:`ovid2.cli.__main__`.

    Delegates to the shared health runner. ``args.command`` stays
    ``dq_checks`` so the docker-argv builder emits the ``dq_checks`` token
    (and keeps ``/data`` mounted read-only), while the container-side run
    reuses the health check machinery verbatim.
    """
    from ovid2.core.docker import is_inside_docker

    args.command = "dq_checks"

    if getattr(args, "list_packages", False):
        from ovid2.cli.health import _list_packages

        return _list_packages()

    # Default to the latest cycle package when the operator selected no
    # checks / parts / packages, so a bare run does something sensible.
    parts = getattr(args, "parts", None)
    parts_is_default = parts is None or parts == ["all"] or parts == "all"
    if not getattr(args, "packages", None) and not getattr(args, "checks", None) and parts_is_default:
        from ovid2.transformations.quality.registry import latest_package

        latest = latest_package()
        if latest is not None:
            args.packages = [latest]

    if not is_inside_docker():
        return _run_host_mode(args)
    return _run_container_mode(args)


if __name__ == "__main__":
    raise SystemExit(
        "This module is a subcommand plugin; run it via `ovid2 dq_checks`."
    )
