"""``ovid2 pipeline`` — inspect and validate pipeline packages.

Read-only commands (no pipeline data is written back). The command
locates every ``ovid2_pipelines.<pipeline>`` package on ``sys.path`` and
reports:

- ``ovid2 pipeline list``     — every pipeline + its declared mapper type.
- ``ovid2 pipeline validate`` — loads every YAML/Python spec and reports errors.
- ``ovid2 pipeline tables``   — expands the canonical table list for a pipeline.

``ovid2 pipeline`` is kept as a hidden, deprecated alias for one release.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from pathlib import Path

from ovid2.core.loaders import get_all_tables
from ovid2.formatters.spec import SpecNotFoundError, SpecValidationError, find_and_load


def _discover_pipelines() -> list[str]:
    """Return every pipeline name found on ``sys.path`` via ``ovid2_pipelines``."""
    try:
        pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []
    names: list[str] = []
    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.ispkg:
            names.append(module_info.name)
    return sorted(names)


def _load_pipeline_settings(pipeline: str) -> dict[str, object]:
    """Return the pipeline's ``pipeline_settings`` module as a plain dict."""
    try:
        mod = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        # Back-compat: an un-migrated tree may still carry ``pipeline_settings``.
        try:
            mod = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
        except ModuleNotFoundError:
            return {}
    return {
        name: getattr(mod, name)
        for name in dir(mod)
        if (not name.startswith("_") and name.isupper()) or name == "mappers_type"
    }


def _iter_pipeline_spec_names(pipeline: str) -> list[str]:
    """Return every table name that has a spec on disk or as a Python module."""
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return []
    tables: set[str] = set()
    for base in getattr(pkg, "__path__", []):
        formatters_dir = Path(base) / "formatters"
        if formatters_dir.is_dir():
            for entry in formatters_dir.iterdir():
                if entry.suffix == ".yaml" or (
                    entry.suffix == ".py" and not entry.name.startswith("_")
                ):
                    tables.add(entry.stem)
    return sorted(tables)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _add_actions(parser: argparse.ArgumentParser, *, command: str) -> None:
    """Attach the shared list/validate/tables actions under ``parser``.

    ``command`` is the dispatch key stored in ``_command`` (``pipeline`` for
    the canonical command, ``partner`` for the deprecated alias).
    """
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    lst = action.add_parser("list", help="List every discoverable pipeline.")
    lst.set_defaults(_command=command, _entry=_entry_list)

    validate = action.add_parser("validate", help="Load every pipeline's specs and report errors.")
    validate.add_argument(
        "--pipeline",
        "--partner",
        dest="pipeline",
        default=None,
        help="Restrict to one pipeline. '--partner' is a deprecated alias.",
    )
    validate.set_defaults(_command=command, _entry=_entry_validate)

    tables = action.add_parser("tables", help="Print the canonical table list for a pipeline.")
    tables.add_argument("pipeline", help="Pipeline name.")
    tables.set_defaults(_command=command, _entry=_entry_tables)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``pipeline`` subcommand plus the hidden ``partner`` alias.

    The alias must keep a *distinct* name: Python 3.11+ ``argparse`` raises
    ``conflicting subparser`` on a duplicate, which crashed every ``ovid2``
    invocation on 3.11/3.12 after the partner -> pipeline rename.
    """
    parser = subparsers.add_parser("pipeline", help="Inspect and validate pipeline packages.")
    _add_actions(parser, command="pipeline")

    # Deprecated alias: ``ovid2 partner`` behaves identically for one release.
    # Omitting ``help=`` keeps it out of the top-level command listing.
    alias = subparsers.add_parser("partner")
    _add_actions(alias, command="partner")


def _entry_list(_args: argparse.Namespace) -> int:
    """List every pipeline package + declared mapper type."""
    pipelines = _discover_pipelines()
    if not pipelines:
        print("No 'ovid2_pipelines.*' packages on sys.path.")
        return 1
    for pipeline in pipelines:
        settings = _load_pipeline_settings(pipeline)
        mapper = settings.get("mappers_type", "?")
        print(f"  {pipeline:<20}  mappers={mapper}")
    return 0


def _entry_validate(args: argparse.Namespace) -> int:
    """Load every spec and print a summary."""
    one = getattr(args, "pipeline", None)
    pipelines = [one] if one else _discover_pipelines()
    if not pipelines:
        print("Nothing to validate.")
        return 1
    total_ok = 0
    total_fail = 0
    for pipeline in pipelines:
        tables = _iter_pipeline_spec_names(pipeline)
        if not tables:
            print(f"[{pipeline}] no specs found")
            continue
        for table in tables:
            try:
                spec = find_and_load(pipeline, table)
            except SpecNotFoundError as exc:
                print(f"[{pipeline}/{table}] NOT FOUND: {exc}")
                total_fail += 1
                continue
            except SpecValidationError as exc:
                print(f"[{pipeline}/{table}] INVALID: {exc}")
                total_fail += 1
                continue
            total_ok += 1
            print(f"[{pipeline}/{table}] OK  ({spec.adapter})")
    print(f"\n{total_ok} ok, {total_fail} failed")
    return 0 if total_fail == 0 else 1


def _entry_tables(args: argparse.Namespace) -> int:
    """Print the canonical table list for a pipeline (based on mapper type)."""
    try:
        for table in get_all_tables(args.pipeline):
            print(table)
    except ValueError as exc:
        print(f"[ovid2 pipeline] {exc}")
        return 1
    return 0
