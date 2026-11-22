"""Contract: destructive delete endpoints pass through the approval gate.

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
an untrusted agent may propose destructive operations, but a human must
approve them first.

The set of ``DELETE`` routes is **discovered from the live FastAPI app**,
not from a hand-maintained list, so a new delete endpoint cannot silently
skip the gate: every ``/api/v1`` ``DELETE`` handler must either call
``_gate("<action>", ...)`` with a literal action listed in
``approvals_svc.DESTRUCTIVE_ACTIONS``, or be explicitly marked
owner-scoped via ``openapi_extra=owner_scoped("<reason>")``
(:mod:`ovid2.web.routes._route_policy`).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")


def _gate_actions(func: ast.AST) -> list[object]:
    """Return the first-argument value of every ``_gate(...)`` call.

    A literal string is returned as-is; any non-literal first argument is
    returned as the sentinel ``None`` so the caller can reject it.
    """
    out: list[object] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_gate"
        ):
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.append(first.value)
            else:
                out.append(None)
    return out


def _endpoint_ast(endpoint: Callable[..., Any]) -> ast.AST:
    """Parse the source of a route endpoint function."""
    source = textwrap.dedent(inspect.getsource(endpoint))
    return ast.parse(source)


def _delete_routes(app: Any) -> list[Any]:
    """Return every ``/api/v1`` APIRoute whose methods include DELETE."""
    from tests.contract._routes import iter_api_routes

    return [
        r
        for r, _deps in iter_api_routes(app)
        if "DELETE" in r.methods and r.path.startswith("/api/v1/")
    ]


def _violations(app: Any) -> list[str]:
    """Return a human-readable list of ungated, unmarked DELETE routes."""
    from ovid2.web.routes._route_policy import OWNER_SCOPED_KEY
    from ovid2.web.services.approvals_svc import DESTRUCTIVE_ACTIONS

    problems: list[str] = []
    for route in _delete_routes(app):
        label = f"DELETE {route.path} ({route.endpoint.__name__})"
        reason = (route.openapi_extra or {}).get(OWNER_SCOPED_KEY)
        tree = _endpoint_ast(route.endpoint)
        actions = _gate_actions(tree)
        if reason is not None:
            if not str(reason).strip():
                problems.append(f"{label}: owner_scoped() reason is empty")
            if actions:
                problems.append(f"{label}: owner-scoped route must not also call _gate")
            continue
        if not actions:
            problems.append(f"{label}: neither calls _gate(...) nor is owner_scoped(...)")
            continue
        for action in actions:
            if action is None:
                problems.append(f"{label}: _gate action must be a string literal")
            elif action not in DESTRUCTIVE_ACTIONS:
                problems.append(f"{label}: _gate action {action!r} not in DESTRUCTIVE_ACTIONS")
    return problems


@pytest.mark.contract
def test_every_delete_route_is_gated_or_owner_scoped() -> None:
    """Every discovered ``/api/v1`` DELETE route is gated or marked owner-scoped."""
    from ovid2.web.app import create_app

    app = create_app()
    routes = _delete_routes(app)
    assert len(routes) >= 10, "route discovery found suspiciously few DELETE routes"
    problems = _violations(app)
    assert not problems, (
        "Destructive DELETE routes must call the approval gate `_gate(...)` "
        "or be marked owner_scoped(reason) "
        "(adr-harness-api-executor-trust-model.md):\n" + "\n".join(problems)
    )


@pytest.mark.contract
def test_discovery_catches_an_ungated_delete() -> None:
    """Negative control: a fresh ungated DELETE route is reported."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.delete("/api/v1/widgets/{name}")
    def api_delete_widget(name: str) -> None:  # pragma: no cover - never called
        """Ungated on purpose."""

    @app.delete("/api/v1/gadgets/{name}")
    def api_delete_gadget(name: str) -> None:  # pragma: no cover - never called
        """Gated with an unknown action."""
        _gate("gadget.delete", name)  # type: ignore[name-defined]  # noqa: F821

    problems = _violations(app)
    assert any("api_delete_widget" in p for p in problems), problems
    assert any("gadget.delete" in p for p in problems), problems


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

    assert {
        "run.overwrite",
        "dataspace.agent_row_access",
        "workflow.data_probe",
    } <= DESTRUCTIVE_ACTIONS


#: Handlers that write a workflow definition — an agent's filtered data
#: probe must pass the ``workflow.data_probe`` gate.
WORKFLOW_WRITE_HANDLERS: frozenset[str] = frozenset(
    {"api_create_workflow", "api_put_workflow", "api_create_workflow_draft"}
)


@pytest.mark.contract
def test_agent_workflow_probes_are_gated(src_root: Path) -> None:
    """Every workflow-writing handler routes agent probes through the gate."""
    api = src_root / "web" / "routes" / "api_v1.py"
    tree = ast.parse(api.read_text(encoding="utf-8"), filename=str(api))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    gate = funcs.get("_gate_agent_workflow_overwrite")
    assert gate is not None and _calls(gate, "_gate")
    assert "AGENT_WORKFLOW_PROBE_ACTION" in ast.unparse(gate)
    for name in WORKFLOW_WRITE_HANDLERS:
        assert name in funcs, name
        assert _calls(funcs[name], "_gate_agent_workflow_overwrite"), name


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
