"""``ovid2 analysis`` — analysis-only pipeline runs.

Runs the same pipeline as ``ovid2 etl`` but with the analysis
transformers (``monthly_counts``, ``distinct_values``, or
``raw_distinct_values``) instead of the map/fix chain. The output
address is used for the primary write; analysis transformers emit their
findings as log events on the bus.
"""

from __future__ import annotations

import argparse

from ovid2.cli.etl import _run_container_mode, _run_host_mode


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``analysis`` subcommand."""
    parser = subparsers.add_parser(
        "analysis",
        help="Run analysis jobs (monthly_counts, distinct_values).",
        description=(
            "Run a pipeline pipeline with an analysis transformer chain. "
            "Emits per-column summaries on the event bus."
        ),
    )
    parser.set_defaults(_command="analysis", _entry=_entry)

    parser.add_argument("--input_address", required=True)
    parser.add_argument("--output_address", required=True)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--batch_id", required=True)
    parser.add_argument("--tables", nargs="+", default=["all"])
    parser.add_argument(
        "--transformations",
        nargs="+",
        default=["format", "monthly_counts", "distinct_values"],
        help=(
            "Analysis chain. Legal values: format, monthly_counts, "
            "distinct_values, raw_distinct_values."
        ),
    )
    parser.add_argument("--engine", default="pyspark")
    parser.add_argument("--workers", type=int, default=25)
    parser.add_argument("--sf_database", default=None)
    parser.add_argument("--sf_account", default=None)
    parser.add_argument("--sf_schema", default=None)
    parser.add_argument("--stderr-log", action="store_true")


def _entry(args: argparse.Namespace) -> int:
    """Reuse the ETL entry-point machinery — same pipeline, different transformers."""
    from ovid2.core.docker import is_inside_docker

    args.command = "analysis"
    if not is_inside_docker():
        return _run_host_mode(args)
    return _run_container_mode(args)
