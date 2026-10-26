"""Approval gate for destructive operations (Phase 3).

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
an untrusted agent may *propose* destructive operations, but a human must
approve them before they execute. This service owns the ``web_approvals``
table and the small state machine:

    pending --approve--> approved
            --deny-----> denied

The typical flow for a gated capability:

1. A request handler calls :func:`require_approval(action, target)`.
2. If no *approved* row exists for that ``(action, target)``, the
   service records a ``pending`` row (idempotently) and raises
   :class:`ApprovalRequired`. The caller surfaces this to the agent as
   "awaiting approval" (HTTP 202).
3. A human approves via the ``/approvals`` UI or the REST endpoint,
   flipping the row to ``approved``.
4. The agent retries; :func:`require_approval` now finds the approved row,
   marks it *consumed* (so approval is single-use), and lets the caller
   proceed.

Approvals are single-use by default: consuming an approved row flips it
back out of the actionable set so a stale approval cannot be replayed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from ovid2.web import db as web_db
from ovid2.web.models import ApprovalDecision, ApprovalListResponse, ApprovalSummary

__all__ = [
    "DESTRUCTIVE_ACTIONS",
    "ApprovalRequired",
    "decide",
    "get_approval",
    "list_approvals",
    "require_approval",
]

#: Actions that must pass through the human-approval gate. Extend as new
#: destructive capabilities are added.
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "secret.delete",
        "connection.delete",
        "engine.delete",
        "pipeline.delete",
        "address.delete",
        "llm.delete",
        # An agent (MCP / assistant) confirming that a run may overwrite an
        # occupied output location. The address is the exact write
        # location, so this replaces existing data — a human must approve.
        "run.overwrite",
    }
)


class ApprovalRequired(Exception):
    """Raised when a destructive op has no approved gate row yet.

    Carries the ``approval_id`` of the pending row so the caller can
    tell the agent which request to ask a human to approve.
    """

    def __init__(self, approval_id: str, action: str, target: str | None) -> None:
        self.approval_id = approval_id
        self.action = action
        self.target = target
        super().__init__(
            f"Operation '{action}' on '{target}' requires human approval "
            f"(approval_id={approval_id})."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_summary(row: sqlite3.Row) -> ApprovalSummary:
    return ApprovalSummary(
        approval_id=row["approval_id"],
        created_at=row["created_at"],
        requested_by=row["requested_by"],
        action=row["action"],
        target=row["target"],
        status=row["status"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        reason=row["reason"],
    )


def list_approvals(status: str | None = None) -> ApprovalListResponse:
    """Return approval rows, optionally filtered by status."""
    query = "SELECT * FROM web_approvals"
    params: tuple[object, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC"
    with web_db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return ApprovalListResponse(approvals=[_row_to_summary(r) for r in rows])


def get_approval(approval_id: str) -> ApprovalSummary | None:
    """Return a single approval row, or ``None`` when it does not exist."""
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    return _row_to_summary(row) if row is not None else None


def require_approval(
    action: str,
    target: str | None,
    *,
    requested_by: str = "web",
    detail: dict[str, object] | None = None,
) -> None:
    """Enforce the human-approval gate for a destructive operation.

    If ``action`` is not destructive, returns immediately. Otherwise:

    - If an ``approved`` row exists for ``(action, target)``, consumes it
      (marks it ``consumed``) and returns — the caller may proceed.
    - Otherwise records/keeps a ``pending`` row and raises
      :class:`ApprovalRequired`.

    Raises:
        ApprovalRequired: When no approved row is available.
    """
    if action not in DESTRUCTIVE_ACTIONS:
        return

    with web_db.connect() as conn:
        approved = conn.execute(
            "SELECT approval_id FROM web_approvals "
            "WHERE action = ? AND (target IS ? OR target = ?) "
            "AND status = 'approved' ORDER BY decided_at DESC LIMIT 1",
            (action, target, target),
        ).fetchone()
        if approved is not None:
            conn.execute(
                "UPDATE web_approvals SET status = 'consumed', "
                "decided_at = ? WHERE approval_id = ?",
                (_now(), approved["approval_id"]),
            )
            return

        # No approval yet — reuse an existing pending row or create one.
        pending = conn.execute(
            "SELECT approval_id FROM web_approvals "
            "WHERE action = ? AND (target IS ? OR target = ?) "
            "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (action, target, target),
        ).fetchone()
        if pending is not None:
            approval_id = pending["approval_id"]
        else:
            approval_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO web_approvals "
                "(approval_id, created_at, requested_by, action, target, "
                "detail_json, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (
                    approval_id,
                    _now(),
                    requested_by,
                    action,
                    target,
                    json.dumps(detail or {}),
                ),
            )

    web_db.audit(
        actor=requested_by,
        action="approval.requested",
        target=f"{action}:{target}",
        detail_json=json.dumps({"approval_id": approval_id}),
    )
    raise ApprovalRequired(approval_id, action, target)


def decide(
    approval_id: str,
    approve: bool,
    payload: ApprovalDecision,
) -> ApprovalSummary:
    """Approve or deny a pending approval row.

    Raises:
        KeyError: When the approval does not exist.
        ValueError: When the row is not in ``pending`` state.
    """
    new_status = "approved" if approve else "denied"
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Approval '{approval_id}' not found.")
        if row["status"] != "pending":
            raise ValueError(f"Approval '{approval_id}' is '{row['status']}', not pending.")
        conn.execute(
            "UPDATE web_approvals SET status = ?, decided_at = ?, "
            "decided_by = ?, reason = ? WHERE approval_id = ?",
            (new_status, _now(), payload.decided_by, payload.reason, approval_id),
        )

    web_db.audit(
        actor=payload.decided_by,
        action=f"approval.{new_status}",
        target=f"{row['action']}:{row['target']}",
        detail_json=json.dumps({"approval_id": approval_id, "reason": payload.reason}),
    )
    result = get_approval(approval_id)
    assert result is not None  # just wrote it
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes.")
