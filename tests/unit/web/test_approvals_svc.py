"""Unit tests for the destructive-op approval gate (Phase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web.models import ApprovalDecision
from ovid2.web.services import approvals_svc


@pytest.fixture(autouse=True)
def _schema(isolated_xdg: Path) -> None:
    """Ensure the web schema (incl. web_approvals) exists per test."""
    web_db.ensure_schema()


@pytest.mark.unit
def test_non_destructive_action_is_not_gated() -> None:
    """A non-destructive action returns immediately, no row created."""
    approvals_svc.require_approval("connection.write", "x")
    assert approvals_svc.list_approvals().approvals == []


@pytest.mark.unit
def test_destructive_action_creates_pending_and_raises() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as ei:
        approvals_svc.require_approval("secret.delete", "my_secret")
    approval_id = ei.value.approval_id

    rows = approvals_svc.list_approvals().approvals
    assert len(rows) == 1
    assert rows[0].approval_id == approval_id
    assert rows[0].status == "pending"
    assert rows[0].action == "secret.delete"
    assert rows[0].target == "my_secret"


@pytest.mark.unit
def test_repeated_request_reuses_pending_row() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as e1:
        approvals_svc.require_approval("secret.delete", "s1")
    with pytest.raises(approvals_svc.ApprovalRequired) as e2:
        approvals_svc.require_approval("secret.delete", "s1")
    assert e1.value.approval_id == e2.value.approval_id
    assert len(approvals_svc.list_approvals(status="pending").approvals) == 1


@pytest.mark.unit
def test_approve_then_require_consumes_and_proceeds() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as ei:
        approvals_svc.require_approval("secret.delete", "s2")
    approval_id = ei.value.approval_id

    decided = approvals_svc.decide(approval_id, True, ApprovalDecision(decided_by="alice"))
    assert decided.status == "approved"

    # Now the gate lets the caller proceed (no raise), consuming the row.
    approvals_svc.require_approval("secret.delete", "s2")
    row = approvals_svc.get_approval(approval_id)
    assert row is not None
    assert row.status == "consumed"


@pytest.mark.unit
def test_consumed_approval_is_single_use() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as ei:
        approvals_svc.require_approval("secret.delete", "s3")
    approvals_svc.decide(ei.value.approval_id, True, ApprovalDecision())
    approvals_svc.require_approval("secret.delete", "s3")  # consumes

    # A second attempt must re-gate (approval was single-use).
    with pytest.raises(approvals_svc.ApprovalRequired):
        approvals_svc.require_approval("secret.delete", "s3")


@pytest.mark.unit
def test_deny_keeps_gate_closed() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as ei:
        approvals_svc.require_approval("connection.delete", "c1")
    decided = approvals_svc.decide(ei.value.approval_id, False, ApprovalDecision())
    assert decided.status == "denied"

    # Still gated — a denied approval is not an approved one.
    with pytest.raises(approvals_svc.ApprovalRequired):
        approvals_svc.require_approval("connection.delete", "c1")


@pytest.mark.unit
def test_decide_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        approvals_svc.decide("nope", True, ApprovalDecision())


@pytest.mark.unit
def test_decide_non_pending_raises_valueerror() -> None:
    with pytest.raises(approvals_svc.ApprovalRequired) as ei:
        approvals_svc.require_approval("engine.delete", "e1")
    approvals_svc.decide(ei.value.approval_id, True, ApprovalDecision())
    with pytest.raises(ValueError):
        approvals_svc.decide(ei.value.approval_id, False, ApprovalDecision())
