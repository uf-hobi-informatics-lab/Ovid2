"""Ovid2 CLI entry point.

Exposes the ``ovid2`` console script (see ``[project.scripts]`` in
``pyproject.toml``). Real subcommand modules register themselves on the
top-level parser through their ``register(subparsers)`` function; anything
not yet implemented remains a stub that prints a friendly "not implemented"
message and exits with status 1.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Sequence

from ovid2 import __version__

# ---------------------------------------------------------------------------
# Subcommand registry
# ---------------------------------------------------------------------------
#
# Each entry is (name, help text, module-with-``register``-or-None).
# When the module reference is None, the parser gets a stub that just
# prints a "not implemented yet" message.

_SUBCOMMANDS: list[tuple[str, str, str | None]] = [
    ("init", "Interactive TUI installer / bootstrap XDG config tree.", "ovid2.installer.init"),
    (
        "doctor",
        "Diagnose environment (docker, jars, snowflake, permissions).",
        "ovid2.installer.doctor",
    ),
    ("image", "Build/push the Ovid2 Docker image.", "ovid2.installer.image"),
    ("pipeline", "Manage pipelines (list, add, validate).", "ovid2.cli.pipeline"),
    ("semantic", "Inspect the semantic layer (vocabularies, bindings).", "ovid2.cli.semantic"),
    (
        "migrate",
        "Migrate legacy pipeline scripts to Ovid2 specs.",
        "ovid2.cli.migrate",
    ),
    ("etl", "Run the ETL pipeline for one or more folders.", "ovid2.cli.etl"),
    ("analysis", "Run analysis jobs (monthly_counts, distinct_values).", "ovid2.cli.analysis"),
    ("dq_checks", "Run data-quality checks (mapping_gap, mapping_report).", "ovid2.cli.dq_checks"),
    ("health", "Run data-health / PCORnet EDC checks and produce a report.", "ovid2.cli.health"),
    ("tools", "Ad-hoc tools (raw_upload, ...).", "ovid2.cli.tools"),
    ("scheduler", "Run the batch scheduler.", "ovid2.cli.scheduler"),
    ("watch", "Attach a TUI viewer to a running instance.", "ovid2.display.renderers.tui"),
    ("service", "Install/manage the systemd service.", "ovid2.installer.service"),
    ("web", "Run the web control panel (FastAPI + HTMX).", "ovid2.web.cli"),
    ("web-worker", "Dispatch ovid2 etl subprocesses from the web queue.", "ovid2.web.worker"),
    ("mcp", "Run the MCP server that wraps the web REST API.", "ovid2.mcp.cli"),
    ("dev", "Run isolated per-developer instances on a shared host.", "ovid2.cli.dev"),
]


logger = logging.getLogger(__name__)


def _add_stub_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> None:
    """Register a placeholder subparser that prints "not implemented"."""
    parser = subparsers.add_parser(name, help=help_text)

    def _stub(_args: argparse.Namespace) -> int:
        print(
            f"[ovid2] '{name}' is not implemented yet. See CHANGELOG.md for the roadmap.",
            file=sys.stderr,
        )
        return 1

    parser.set_defaults(_command=name, _entry=_stub)


def _add_unavailable_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    exc: BaseException,
) -> None:
    """Register a subparser whose backing module failed to import.

    This keeps a single missing *optional* dependency (e.g. ``argon2``
    from the ``[web]`` extra, absent in the lean ETL-executor image) from
    taking down the entire CLI. Every other subcommand — crucially
    ``ovid2 etl`` on a worker — stays usable; only the affected command
    reports the failure, with a pointer to the missing dependency.

    Args:
        subparsers: The parent ``_SubParsersAction`` to attach to.
        name: The subcommand name.
        help_text: The subcommand help string.
        exc: The exception raised while importing the backing module.
    """
    parser = subparsers.add_parser(name, help=help_text)
    reason = str(exc) or exc.__class__.__name__

    def _unavailable(_args: argparse.Namespace) -> int:
        print(
            f"[ovid2] '{name}' is unavailable in this environment: {reason}. "
            "It likely requires an optional dependency (e.g. the ovid2[web] "
            "extra) that is not installed in this image.",
            file=sys.stderr,
        )
        return 1

    parser.set_defaults(_command=name, _entry=_unavailable)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser with every subcommand.

    Subcommand modules are imported so they can register their own
    arguments. A module that fails to import because of a missing
    *optional* dependency degrades to an "unavailable" stub instead of
    aborting the whole CLI — see :func:`_add_unavailable_subparser`. This
    is what lets ``ovid2 etl`` run inside the lean executor image, which
    intentionally omits the ``[web]`` extra (and therefore ``argon2``)
    per the harness/executor trust split in ``AGENTS.md``.
    """
    parser = argparse.ArgumentParser(
        prog="ovid2",
        description="Ovid2 — data processing engine (PCORnet CDM v7.0, OMOP CDM 5.3).",
    )
    parser.add_argument("--version", action="version", version=f"ovid2 {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (repeatable).",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    for name, help_text, module_name in _SUBCOMMANDS:
        if module_name is None:
            _add_stub_subparser(subparsers, name, help_text)
            continue
        try:
            module = importlib.import_module(module_name)
            module.register(subparsers)
        except ImportError as exc:
            logger.debug("subcommand %r unavailable: %s", name, exc)
            _add_unavailable_subparser(subparsers, name, help_text, exc)
    return parser


def _configure_logging(verbosity: int) -> None:
    """Configure root logging.

    Verbosity levels:

    - 0: WARNING
    - 1: INFO
    - 2+: DEBUG
    """
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point.

    Args:
        argv: Argument vector for testing. When ``None``, uses :data:`sys.argv`.

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))

    entry = getattr(args, "_entry", None)
    if entry is None:
        parser.print_help(sys.stderr)
        return 2
    return int(entry(args))


if __name__ == "__main__":
    raise SystemExit(main())
