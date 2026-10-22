"""Shared run-of-items model for the Python terminal renderers.

Every Ovid2 run — whether an ETL run or a data-health / ``dq_checks``
run — is fundamentally "execute a pipeline made of N *items*, each of
which starts, then completes or fails, with running ok/failed counts."
The two run kinds differ only in what an item *is*:

- ETL runs: one item per CDM **table** (``table.*`` events), with a
  per-table fixer chain (``fix.*`` events).
- health / ``dq_checks`` runs: one item per data-health **check**
  (``check.*`` events), grouped by CDC part, with no fixer chain.

This module is the Python-side analogue of the web tier's
``display/run/panel.js`` ``mountRunPanel`` config: a single
:class:`RunAggregate` folds an event stream into generic state, driven
by an :class:`ItemKind` config selected per run kind. The renderers
(``tui``, ``plain_log``, ``email_digest``) all consume it so tables and
checks render with the same tabulation, colours and counts.

Note: ``check.completed`` may carry ``check_status="exception"`` (the
data violated the rule) which is distinct from ``check.failed`` (the
check operator itself crashed). For the terminal renderers both count
as a non-OK item — a two-bucket ok/failed model, matching ETL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ItemKind:
    """Per-run-kind wiring describing what a pipeline *item* is.

    Attributes:
        label: Singular human label for one item (``"table"`` / ``"check"``).
        started_kind: Event kind emitted when an item begins.
        completed_kind: Event kind emitted when an item finishes.
        failed_kind: Event kind emitted when an item hard-fails.
        id_field: Payload field carrying the item's identity.
        name_field: Optional payload field carrying a human name (checks).
        group_field: Optional payload field to group items by (checks -> part).
        status_field: Optional payload field on ``completed`` carrying a
            status string; a value in ``failed_status_values`` counts the
            completed item as non-OK.
        failed_status_values: Status values (on ``completed``) that count as
            a non-OK item even though the event kind is "completed".
        shows_fixes: Whether this run kind emits a per-item fixer chain.
    """

    label: str
    started_kind: str
    completed_kind: str
    failed_kind: str
    id_field: str
    name_field: str | None = None
    group_field: str | None = None
    status_field: str | None = None
    failed_status_values: frozenset[str] = frozenset()
    shows_fixes: bool = False


TABLE_ITEMS = ItemKind(
    label="table",
    started_kind="table.started",
    completed_kind="table.completed",
    failed_kind="table.failed",
    id_field="table",
    shows_fixes=True,
)

CHECK_ITEMS = ItemKind(
    label="check",
    started_kind="check.started",
    completed_kind="check.completed",
    failed_kind="check.failed",
    id_field="check_id",
    name_field="check_name",
    group_field="part",
    status_field="check_status",
    failed_status_values=frozenset({"exception", "error"}),
)

_ALL_KINDS: tuple[ItemKind, ...] = (TABLE_ITEMS, CHECK_ITEMS)

_ITEM_EVENT_KINDS: frozenset[str] = frozenset(
    kind
    for item in _ALL_KINDS
    for kind in (item.started_kind, item.completed_kind, item.failed_kind)
)


def is_item_event(kind: str) -> bool:
    """Return whether ``kind`` is a table/check start/complete/fail event."""
    return kind in _ITEM_EVENT_KINDS


def pick_item_kind(records: Iterable[Mapping[str, Any]]) -> ItemKind:
    """Detect the run kind from the first item event in ``records``.

    Mirrors the web tier's DOM-based discriminator (``live.js``): the
    run kind is inferred from which item events appear, defaulting to
    :data:`TABLE_ITEMS` when no item event has been seen yet.

    Args:
        records: An iterable of event dicts.

    Returns:
        The matching :class:`ItemKind` (``TABLE_ITEMS`` if undetermined).
    """
    for record in records:
        if not isinstance(record, Mapping):
            continue
        kind = str(record.get("kind", ""))
        for item_kind in _ALL_KINDS:
            if kind in {
                item_kind.started_kind,
                item_kind.completed_kind,
                item_kind.failed_kind,
            }:
                return item_kind
    return TABLE_ITEMS


@dataclass
class RunAggregate:
    """Generic per-run state folded from an event stream.

    A single :meth:`apply` call switches on the configured
    :class:`ItemKind` event kinds, so the same aggregate backs both ETL
    and health runs. Renderers read the public fields directly.
    """

    item_kind: ItemKind = field(default=TABLE_ITEMS)
    instance_id: str = ""
    pipeline: str = ""
    batch_id: str = ""
    current_item: str = ""
    current_group: str = ""
    stage: str = ""
    fix: str = ""
    fix_progress: str = ""
    items_ok: int = 0
    items_failed: int = 0
    recent_errors: list[str] = field(default_factory=list)
    started_at: str = ""
    last_event_ts: str = ""
    finished: bool = False
    finish_kind: str = ""

    def _push_error(self, msg: str) -> None:
        self.recent_errors.append(msg)
        self.recent_errors[:] = self.recent_errors[-5:]

    def apply(self, record: Mapping[str, Any]) -> None:
        """Update state from one event record."""
        kind = str(record.get("kind", ""))
        self.last_event_ts = str(record.get("ts", ""))
        if not self.instance_id:
            self.instance_id = str(record.get("instance_id", ""))

        ik = self.item_kind

        if kind == "instance.started":
            self.started_at = self.last_event_ts
            self.pipeline = str(record.get("pipeline") or self.pipeline)
            return
        if kind == "instance.finished":
            self.finished = True
            extra = record.get("extra") or {}
            success = bool(extra.get("success", True)) if isinstance(extra, Mapping) else True
            self.finish_kind = "success" if success else "failure"
            return

        if kind in {ik.started_kind, ik.completed_kind, ik.failed_kind}:
            self._apply_item_event(kind, record)
            return

        if ik.shows_fixes and kind in {"fix.applied", "fix.failed"}:
            self._apply_fix_event(kind, record)
            return

        if kind == "stage.progress":
            self.stage = str(record.get("stage") or self.stage)
            return

        if kind in {"warning", "error"}:
            self._push_error(f"{kind}: {record.get('message', '')}")

    def _apply_item_event(self, kind: str, record: Mapping[str, Any]) -> None:
        ik = self.item_kind
        self.pipeline = str(record.get("pipeline") or self.pipeline)
        self.batch_id = str(record.get("batch_id") or self.batch_id)
        item = str(record.get(ik.id_field) or self.current_item)
        self.current_item = item
        if ik.group_field:
            self.current_group = str(record.get(ik.group_field) or self.current_group)

        if kind == ik.completed_kind:
            failed = False
            status_field = ik.status_field
            if status_field:
                status = str(record.get(status_field) or "")
                failed = status in ik.failed_status_values
                if failed:
                    self.items_failed += 1
                    self._push_error(f"{item}: {status}")
                else:
                    self.items_ok += 1
            else:
                self.items_ok += 1
            self._clear_item_scratch()
        elif kind == ik.failed_kind:
            self.items_failed += 1
            self._push_error(f"{item}: {record.get('error', '')}")
            self._clear_item_scratch()

    def _apply_fix_event(self, kind: str, record: Mapping[str, Any]) -> None:
        self.fix = str(record.get("fix") or "")
        current = record.get("current_fix", 0)
        total = record.get("total_fixes", 0)
        self.fix_progress = f"{current}/{total}" if total else ""
        if kind == "fix.failed":
            self._push_error(f"fix {self.fix} on {self.current_item}: {record.get('error', '')}")

    def _clear_item_scratch(self) -> None:
        self.fix = ""
        self.fix_progress = ""
        self.stage = ""
