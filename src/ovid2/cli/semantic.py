"""``ovid2 semantic`` — inspect the semantic layer.

Read-only commands (the semantic registry is never modified from the
CLI). Provides:

- ``ovid2 semantic list [--kind vocabulary]``  — every registered handle.
- ``ovid2 semantic show <kind> <name>``        — one handle, in detail.
- ``ovid2 semantic bindings [--cdm <name>]``   — every registered
  :class:`~ovid2.semantic.registry.ColumnBinding`.

The command is deliberately thin — it exercises the same public API
that mapper ops and adapters consume, which makes it a useful smoke test
for the registry itself.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass

import ovid2.semantic as semantic
from ovid2.semantic.registry import registered_kinds

# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``semantic`` subcommand and its actions."""
    parser = subparsers.add_parser("semantic", help="Inspect the semantic layer.")
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    lst = action.add_parser("list", help="List registered semantic handles.")
    lst.add_argument(
        "--kind",
        default=None,
        help="Restrict to one kind (e.g. `vocabulary`). Default: all kinds.",
    )
    lst.set_defaults(_command="semantic", _entry=_entry_list)

    show = action.add_parser("show", help="Show one semantic handle in detail.")
    show.add_argument("kind", help="Handle kind (e.g. `vocabulary`).")
    show.add_argument("name", help="Handle name (e.g. `omop_concept`).")
    show.set_defaults(_command="semantic", _entry=_entry_show)

    bindings = action.add_parser("bindings", help="List registered column bindings.")
    bindings.add_argument(
        "--cdm",
        default=None,
        help="Restrict to one CDM (e.g. `pcornet_7_0`). Default: all.",
    )
    bindings.add_argument(
        "--table",
        default=None,
        help="Restrict to one table (requires --cdm).",
    )
    bindings.set_defaults(_command="semantic", _entry=_entry_bindings)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def _entry_list(args: argparse.Namespace) -> int:
    """List every registered handle, optionally filtered by kind."""
    kinds = [args.kind] if args.kind else registered_kinds()
    if not kinds:
        print("No semantic handles registered.")
        return 0
    total = 0
    for kind in kinds:
        names = semantic.registered(kind)
        if not names:
            print(f"[{kind}] (none)")
            continue
        print(f"[{kind}]")
        for name in names:
            handle = semantic.get(kind, name)
            version = getattr(handle, "version", "") or "-"
            print(f"  {name:<24}  v={version}")
            total += 1
    print(f"\n{total} handle(s).")
    return 0


def _entry_show(args: argparse.Namespace) -> int:
    """Show one handle in detail."""
    try:
        handle = semantic.get(args.kind, args.name)
    except ValueError as exc:
        print(f"[ovid2 semantic] {exc}")
        return 1
    print(f"{handle.kind}:{handle.name}")
    if not is_dataclass(handle):
        return 0
    for f in fields(handle):
        value = getattr(handle, f.name)
        print(f"  {f.name:<24}  {value!r}")
    return 0


def _entry_bindings(args: argparse.Namespace) -> int:
    """List registered column bindings."""
    if args.table and not args.cdm:
        print("[ovid2 semantic] --table requires --cdm.")
        return 1
    if args.cdm:
        bindings = semantic.bindings_for(args.cdm, table=args.table)
    else:
        bindings = semantic.all_bindings()
    if not bindings:
        print("No column bindings registered.")
        return 0
    for b in bindings:
        role = b.role or "-"
        req = "required" if b.required else "optional"
        print(f"  {b.cdm}.{b.table}.{b.column:<24}  -> {b.vocabulary:<20}  role={role:<20}  {req}")
    print(f"\n{len(bindings)} binding(s).")
    return 0
