"""Contract: destructive delete endpoints pass through the approval gate.

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
an untrusted agent may propose destructive operations, but a human must
approve them first. This test guards that every ``DELETE`` handler under
``web/routes/api_v1.py`` for a gated resource calls ``_gate(...)`` — so a
future delete endpoint cannot silently skip the approval gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Delete handler function names that must invoke the approval gate.
GATED_DELETE_HANDLERS: frozenset[str] = frozenset(
    {
        "api_delete_secret",
        "api_delete_connection",
        "api_delete_engine",
        "api_delete_pipeline_config",
        "api_delete_address",
        "api_delete_llm",
    }
)


def _calls_gate(func: ast.FunctionDef) -> bool:
    """Return True if the function body calls ``_gate(...)``."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_gate"
        ):
            return True
    return False


@pytest.mark.contract
def test_destructive_deletes_are_gated(src_root: Path) -> None:
    """Every gated delete handler must call the approval gate."""
    api = src_root / "web" / "routes" / "api_v1.py"
    if not api.is_file():
        pytest.skip("Web API routes not present.")

    tree = ast.parse(api.read_text(encoding="utf-8"), filename=str(api))
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in GATED_DELETE_HANDLERS:
            found[node.name] = _calls_gate(node)

    missing_handlers = GATED_DELETE_HANDLERS - set(found)
    assert not missing_handlers, (
        f"Expected gated delete handlers not found in api_v1.py: {sorted(missing_handlers)}"
    )
    ungated = sorted(name for name, gated in found.items() if not gated)
    assert not ungated, (
        "These destructive delete handlers do not call the approval "
        "gate `_gate(...)` (see "
        "adr-harness-api-executor-trust-model.md):\n" + "\n".join(ungated)
    )


#: Handlers that return Data Space *row values* — agents must pass the
#: ``dataspace.agent_row_access`` approval gate.
ROW_RETURNING_HANDLERS: frozenset[str] = frozenset(
    {
        "api_ods_table_sample",
        "api_ods_table_export",
        "api_ods_table_export_job",
        "api_job_artifact",
    }
)


def _calls(func: ast.FunctionDef, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        for n in ast.walk(func)
    )


@pytest.mark.contract
def test_agent_row_access_is_gated(src_root: Path) -> None:
    """Every row-returning handler routes agents through the approval gate."""
    api = src_root / "web" / "routes" / "api_v1.py"
    tree = ast.parse(api.read_text(encoding="utf-8"), filename=str(api))
    found = {
        n.name: _calls(n, "_gate_agent_row_access")
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in ROW_RETURNING_HANDLERS
    }
    assert set(found) == ROW_RETURNING_HANDLERS, sorted(ROW_RETURNING_HANDLERS - set(found))
    ungated = sorted(k for k, v in found.items() if not v)
    assert not ungated, f"Row-returning handlers without the agent gate: {ungated}"


@pytest.mark.contract
def test_gated_actions_are_destructive() -> None:
    """The overwrite and agent-row-access actions stay in DESTRUCTIVE_ACTIONS."""
    from ovid2.web.services.approvals_svc import DESTRUCTIVE_ACTIONS

    assert {"run.overwrite", "dataspace.agent_row_access"} <= DESTRUCTIVE_ACTIONS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
