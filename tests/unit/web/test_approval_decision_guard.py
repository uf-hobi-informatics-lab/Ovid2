"""Unit tests for the approval-decision guard (``api_v1._decision_for``).

An approval is the human check on a proposed destructive operation, so:

* agent principals (``mcp:`` / ``llm:``) may never approve or deny;
* an authenticated principal may not decide its own request;
* the recorded ``decided_by`` is the principal, not the client payload
  (except under the auth-disabled single-identity bypass).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from ovid2.web import db as web_db
from ovid2.web.auth.deps import AuthContext
from ovid2.web.auth.roles import Role
from ovid2.web.models import ApprovalDecision
from ovid2.web.routes import api_v1
from ovid2.web.services import approvals_svc


@pytest.fixture(autouse=True)
def _db(isolated_xdg: Path) -> None:
    web_db.ensure_schema()


def _pending(requested_by: str) -> str:
    with pytest.raises(approvals_svc.ApprovalRequired) as info:
        approvals_svc.require_approval("secret.delete", "s1", requested_by=requested_by)
    return info.value.approval_id


def _admin(user_id: str = "u-1", email: str | None = "a@example.org") -> AuthContext:
    return AuthContext(user_id=user_id, role=Role.SYSTEM_ADMIN, email=email)


@pytest.mark.unit
@pytest.mark.parametrize("principal", ["mcp:service", "llm:u-1"])
def test_agent_cannot_decide(principal: str) -> None:
    approval_id = _pending("web")
    ctx = AuthContext(user_id=principal, role=Role.SYSTEM_ADMIN)
    with pytest.raises(HTTPException) as info:
        api_v1._decision_for(ctx, approval_id, ApprovalDecision())
    assert info.value.status_code == 403


@pytest.mark.unit
def test_cannot_decide_own_request() -> None:
    approval_id = _pending("u-1")
    with pytest.raises(HTTPException) as info:
        api_v1._decision_for(_admin("u-1"), approval_id, ApprovalDecision())
    assert info.value.status_code == 403


@pytest.mark.unit
def test_decided_by_comes_from_principal() -> None:
    approval_id = _pending("mcp:service")
    out = api_v1._decision_for(
        _admin("u-2", "boss@example.org"), approval_id, ApprovalDecision(decided_by="spoofed")
    )
    assert out.decided_by == "boss@example.org"


@pytest.mark.unit
def test_auth_disabled_bypass_keeps_payload() -> None:
    approval_id = _pending("web")
    ctx = AuthContext(user_id="anonymous", role=Role.SYSTEM_ADMIN, anonymous_bypass=True)
    out = api_v1._decision_for(ctx, approval_id, ApprovalDecision(decided_by="test"))
    assert out.decided_by == "test"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
