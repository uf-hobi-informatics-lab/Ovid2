"""``ovid2 migrate`` — legacy Ovid pipeline → Ovid2 spec migrator.

Subcommands:

- ``formatter`` — convert one legacy ``*_formatter.py`` to a YAML spec.
- ``pipeline``   — batch-convert every formatter under a legacy pipeline
  directory into ``formatters/*.yaml`` under a destination directory.
- ``compare``   — golden-file regression harness (delegates to
  :func:`ovid2.tools.golden_diff.compare_directories`).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ovid2.tools.formatter_migrator import MigrationError, migrate_file
from ovid2.tools.golden_diff import compare_directories


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``migrate`` subcommand and its actions."""
    parser = subparsers.add_parser("migrate", help="Migrate legacy pipeline scripts to Ovid2 specs.")
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    fmt = action.add_parser("formatter", help="Convert one legacy formatter file.")
    fmt.add_argument("source", help="Path to the legacy *_formatter.py file.")
    fmt.add_argument("--pipeline", required=True, help="Pipeline name (destination package).")
    fmt.add_argument(
        "--destination",
        required=True,
        help="Directory to write the resulting <table>.yaml into.",
    )
    fmt.add_argument("--overwrite", action="store_true", help="Replace an existing spec.")
    fmt.set_defaults(_command="migrate", _entry=_entry_formatter)

    part = action.add_parser("pipeline", help="Batch-convert every formatter under a directory.")
    part.add_argument("legacy_dir", help="Directory containing *_formatter.py files.")
    part.add_argument("--pipeline", required=True, help="Pipeline name (destination package).")
    part.add_argument(
        "--destination",
        required=True,
        help="Directory to write the resulting <table>.yaml files into.",
    )
    part.add_argument("--overwrite", action="store_true", help="Replace existing specs.")
    part.set_defaults(_command="migrate", _entry=_entry_pipeline)

    cmp_ = action.add_parser("compare", help="Golden-file diff between two output directories.")
    cmp_.add_argument("a", help="Legacy Ovid output directory.")
    cmp_.add_argument("b", help="Ovid2 output directory.")
    cmp_.add_argument("--delimiter", default="|")
    cmp_.set_defaults(_command="migrate", _entry=_entry_compare)


def _entry_formatter(args: argparse.Namespace) -> int:
    """Handle ``ovid2 migrate formatter``."""
    try:
        target, analysis = migrate_file(
            Path(args.source),
            pipeline=args.pipeline,
            destination_dir=Path(args.destination),
            overwrite=args.overwrite,
        )
    except MigrationError as exc:
        print(f"[migrate] {args.source}: {exc}")
        return 1
    except FileExistsError as exc:
        print(f"[migrate] {exc}")
        return 1
    print(f"[migrate] wrote {target} (adapter=pcornet_passthrough, table={analysis.table})")
    for warn in analysis.warnings:
        print(f"[migrate]   warning: {warn}")
    return 0


def _entry_pipeline(args: argparse.Namespace) -> int:
    """Handle ``ovid2 migrate pipeline``."""
    legacy_dir = Path(args.legacy_dir)
    if not legacy_dir.is_dir():
        print(f"[migrate] {legacy_dir} is not a directory.")
        return 1

    dest = Path(args.destination)
    dest.mkdir(parents=True, exist_ok=True)

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    for source in sorted(legacy_dir.glob("*_formatter.py")):
        try:
            target, analysis = migrate_file(
                source,
                pipeline=args.pipeline,
                destination_dir=dest,
                overwrite=args.overwrite,
            )
        except (MigrationError, FileExistsError) as exc:
            failures.append((source.name, str(exc)))
            print(f"[migrate] FAIL {source.name}: {exc}")
            continue
        successes.append(analysis.table)
        print(f"[migrate] OK   {source.name} -> {target.name}")
        for warn in analysis.warnings:
            print(f"[migrate]      warning: {warn}")

    print(f"\n{len(successes)} migrated, {len(failures)} failed")
    if failures:
        print("Failures (require manual porting with adapter: custom):")
        for name, reason in failures:
            print(f"  - {name}: {reason}")
    return 0 if not failures else 1


def _entry_compare(args: argparse.Namespace) -> int:
    """Handle ``ovid2 migrate compare``."""
    diff = compare_directories(Path(args.a), Path(args.b), delimiter=args.delimiter)
    for entry in diff.files:
        badge = {"match": "OK  ", "only_a": "MISS", "only_b": "EXTRA"}.get(entry.status, "DIFF")
        print(f"[{badge}] {entry.path}  {entry.detail}")
    print(diff.summary())
    return 0 if diff.match else 1
