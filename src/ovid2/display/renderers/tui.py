"""Rich Live TUI renderer — replaces ``ovid-watch``.

Attaches to a running instance's event log (or the most recently created
one) and renders a live view showing:

- Current pipeline / batch / instance id
- The current *item* (a CDM table for ETL runs, a data-health check for
  health / ``dq_checks`` runs) and its stage / group
- Per-fix progress with a running counter (ETL only)
- Recent warnings / errors
- Overall elapsed time

The run kind is auto-detected from the event stream (see
:func:`ovid2.display.renderers._run_state.pick_item_kind`), so the same
panel renders tables and checks with identical tabulation, colours and
ok/failed counts.

Runs as a plain reader — no Spark dependency. Safe to invoke from any
tty on the host.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.panel import Panel

from ovid2.display.reader import follow_events, latest_event_log
from ovid2.display.renderers._run_state import (
    TABLE_ITEMS,
    RunAggregate,
    pick_item_kind,
)
from ovid2.display.renderers._run_state import (
    is_item_event as _is_item_event,
)

logger = logging.getLogger(__name__)


def _fmt_ts(iso_ts: str) -> str:
    """Trim an ISO-8601 timestamp to ``HH:MM:SS`` for display."""
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts[-8:] if iso_ts else ""


class _State:
    """Live panel state that auto-detects run kind then delegates folding.

    Wraps a :class:`RunAggregate`. Until the first item event is seen the
    run kind is unknown, so any leading records are buffered and replayed
    once :func:`pick_item_kind` resolves the kind. This keeps ``watch``
    agnostic to whether it is attached to an ETL or a health run.
    """

    def __init__(self) -> None:
        self._agg: RunAggregate | None = None
        self._pending: list[Mapping[str, Any]] = []

    def _ensure(self, record: Mapping[str, Any]) -> None:
        if self._agg is not None:
            return
        rec_kind = str(record.get("kind", ""))
        # Only lock in the run kind once we've actually seen an item event;
        # otherwise keep buffering so we don't mislabel a health run as ETL
        # on the strength of its leading instance.started event.
        if not _is_item_event(rec_kind):
            return
        kind = pick_item_kind([record])
        self._agg = RunAggregate(item_kind=kind)
        for buffered in self._pending:
            self._agg.apply(buffered)
        self._pending.clear()

    def apply(self, record: Mapping[str, Any]) -> None:
        """Update state from one event record."""
        self._ensure(record)
        if self._agg is None:
            self._pending.append(record)
            return
        self._agg.apply(record)

    @property
    def aggregate(self) -> RunAggregate:
        """Return a snapshot aggregate for rendering.

        Non-destructive: until an item event has resolved the run kind we
        return a *provisional* ETL-flavoured aggregate replayed from the
        buffered events, without locking ``_agg`` in — otherwise merely
        rendering a frame after ``instance.started`` would mislabel a
        health run as ETL before its first ``check.*`` event arrives.
        """
        if self._agg is not None:
            return self._agg
        provisional = RunAggregate(item_kind=TABLE_ITEMS)
        for buffered in self._pending:
            provisional.apply(buffered)
        return provisional

    @property
    def finished(self) -> bool:
        return self._agg is not None and self._agg.finished

    @property
    def items_failed(self) -> int:
        return self._agg.items_failed if self._agg is not None else 0


def _build_panel(state: _State) -> Panel:
    """Return a Rich Panel showing the current state."""
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    agg = state.aggregate
    ik = agg.item_kind

    body = Table.grid(padding=(0, 1))
    body.add_column(style="dim", justify="right")
    body.add_column()

    body.add_row("instance", agg.instance_id or "?")
    body.add_row("pipeline", agg.pipeline or "?")
    body.add_row("batch", agg.batch_id or "?")
    body.add_row(ik.label, agg.current_item or "?")
    if ik.group_field:
        body.add_row(ik.group_field, agg.current_group or "?")
    else:
        body.add_row("stage", agg.stage or "?")
    if ik.shows_fixes and agg.fix:
        fix_line = f"{agg.fix}  ({agg.fix_progress})" if agg.fix_progress else agg.fix
        body.add_row("fix", fix_line)
    body.add_row(
        f"{ik.label}s",
        f"[green]{agg.items_ok} ok[/]  [red]{agg.items_failed} failed[/]",
    )
    body.add_row("started", _fmt_ts(agg.started_at))
    body.add_row("latest", _fmt_ts(agg.last_event_ts))

    if agg.recent_errors:
        errors = Text("\n".join(f"• {msg}" for msg in agg.recent_errors), style="red")
        body.add_row("errors", errors)

    title = "ovid2 watch"
    if agg.finished:
        border_style = "green" if agg.finish_kind == "success" else "red"
        title = f"ovid2 watch — {agg.finish_kind}"
    else:
        border_style = "cyan"

    return Panel(body, title=title, border_style=border_style)


def _run_tui(path: Path) -> int:
    """Run the Rich Live loop until the pipeline signals ``instance.finished``."""
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError as exc:  # pragma: no cover — rich is a hard dep
        print(f"[ovid2] Rich is not installed: {exc}", file=sys.stderr)
        return 2

    console = Console(highlight=False)
    state = _State()

    console.print(f"[dim]Watching {path}[/]")

    with Live(_build_panel(state), console=console, refresh_per_second=8, transient=False) as live:
        for record in follow_events(path):
            state.apply(record)
            # ``refresh=True`` forces the frame to paint immediately rather
            # than waiting on the throttled auto-refresh; without it the
            # final frame is dropped when we ``break`` on ``instance.finished``
            # before the next refresh tick, leaving a stale/empty panel.
            live.update(_build_panel(state), refresh=True)
            if state.finished:
                break
    return 0 if state.items_failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``watch`` subcommand."""
    parser = subparsers.add_parser(
        "watch",
        help="Attach a TUI viewer to a running instance.",
        description="Follow an Ovid2 event log and render a live status panel.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--instance", help="Instance id to watch.")
    group.add_argument(
        "--log",
        help="Path to a specific JSONL event log (overrides --instance).",
    )
    group.add_argument(
        "--follow-latest",
        action="store_true",
        help="Watch the most recently created event log.",
    )
    parser.set_defaults(_command="watch", _entry=_cli_entry)


def _resolve_log(args: argparse.Namespace) -> Path | None:
    """Resolve which log file to attach to from the CLI arguments."""
    if args.log:
        return Path(args.log)
    if args.instance:
        from ovid2.config.settings import WORK_DIR

        return WORK_DIR / f"events-{args.instance}.jsonl"
    return latest_event_log()


def _cli_entry(args: argparse.Namespace) -> int:
    """Entry point invoked by :mod:`ovid2.cli.__main__`."""
    log_path = _resolve_log(args)
    if log_path is None:
        print(
            "[ovid2] No event log found. Pass --log, --instance, or --follow-latest.",
            file=sys.stderr,
        )
        return 2
    return _run_tui(log_path)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``ovid2-watch``)."""
    parser = argparse.ArgumentParser(
        prog="ovid2-watch",
        description="Follow an Ovid2 event log and render a live status panel.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--instance", help="Instance id to watch.")
    group.add_argument("--log", help="Path to a specific JSONL event log.")
    group.add_argument(
        "--follow-latest",
        action="store_true",
        help="Watch the most recently created event log (default).",
    )
    args = parser.parse_args(argv)
    if not (args.log or args.instance or args.follow_latest):
        args.follow_latest = True
    return _cli_entry(args)


if __name__ == "__main__":
    raise SystemExit(main())
