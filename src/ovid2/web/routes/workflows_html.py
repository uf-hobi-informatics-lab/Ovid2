"""HTML pages for workflows (adr-workflows.md).

- ``GET /workflows`` — the index (cards with a mini DAG, run / duplicate /
  delete / enable actions).
- ``GET /workflows/new`` and ``GET /workflows/{name}`` — the canvas editor
  (``static/workflow_canvas.js``). Everything after first paint goes
  through the JSON API under ``/api/v1/workflows``.
- ``GET /workflows/{name}/runs/{run_id}`` — a live, read-only canvas of
  one run with per-step status and the rollback timeline.
- ``POST /workflows/{name}/delete`` / ``…/duplicate`` / ``…/run`` /
  ``…/enable`` / ``…/disable`` — form fallbacks for the index actions.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ovid2.web.auth.csrf import require_csrf
from ovid2.web.auth.deps import require_capability
from ovid2.web.auth.roles import Capability
from ovid2.web.services import (
    addresses_svc,
    pipelines_svc,
    workflow_runner_svc,
    workflows_svc,
)
from ovid2.web.templating import templates

router = APIRouter()

#: Trigger choices offered on the canvas (U2: a select, never free text).
TRIGGER_TYPES: tuple[str, ...] = ("manual", "cron", "interval", "watch")


def _editor_catalog() -> dict[str, Any]:
    """Pipelines + addresses + kind flags the canvas needs (all names, no secrets)."""
    from ovid2.web.kinds import kind_choices
    from ovid2.web.services._table_expand import _driver_type_for_address

    pipelines = [
        {
            "name": p.name,
            "kind": p.kind,
            "engine": p.engine,
            "engine_type": p.engine_type,
            "description": p.description,
            "input_address": p.input_address,
            "output_address": p.output_address,
            "tables": p.tables,
            "trigger_type": p.trigger_type,
        }
        for p in pipelines_svc.list_pipelines()
    ]
    addresses = []
    for a in addresses_svc.list_addresses():
        driver = _driver_type_for_address(a.name) or a.src_name
        rollback: bool | None
        try:
            from ovid2.drivers.registry import load_class

            rollback = bool(load_class(driver).supports_workflow_rollback()) if driver else None
        except (KeyError, ImportError):
            rollback = None
        addresses.append(
            {"name": a.name, "type": driver, "connection": a.connection, "rollback": rollback}
        )
    from ovid2.config.workflow_conditions import catalogue as condition_catalogue

    return {
        "pipelines": pipelines,
        "addresses": addresses,
        "kinds": kind_choices(),
        "triggers": list(TRIGGER_TYPES),
        "conditions": condition_catalogue(),
    }


def _can(request: Request, cap: str) -> bool:
    from ovid2.web.templating import has_cap

    return bool(has_cap(request, cap))


@router.get(
    "/workflows",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def workflows_page(
    request: Request, flash: str | None = None, error: str | None = None
) -> HTMLResponse:
    """The workflows index."""
    workflows = [w.model_dump(by_alias=True) for w in workflows_svc.list_workflows()]
    kinds = {p.name: p.kind for p in pipelines_svc.list_pipelines()}
    recent = workflow_runner_svc.list_runs(limit=8).runs
    return templates.TemplateResponse(
        request,
        "workflows.html",
        {
            "workflows": workflows,
            "pipeline_kinds": kinds,
            "recent_runs": [r.model_dump() for r in recent],
            "flash": flash,
            "error": error,
            "can_write": _can(request, "manage_config"),
            "can_run": _can(request, "submit_jobs"),
        },
    )


def _editor(request: Request, name: str | None, *, draft: bool = False) -> HTMLResponse:
    detail = workflows_svc.get_workflow(name) if name else None
    if name and detail is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")
    response = templates.TemplateResponse(
        request,
        "workflow_editor.html",
        {
            "name": name or "",
            "workflow": detail.model_dump(by_alias=True) if detail else None,
            "catalog": _editor_catalog(),
            "draft": draft,
            "triggers": TRIGGER_TYPES,
            "can_write": _can(request, "manage_config"),
            "can_run": _can(request, "submit_jobs"),
            "runs": [
                r.model_dump() for r in workflow_runner_svc.list_runs(workflow=name, limit=10).runs
            ]
            if name
            else [],
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/workflows/new",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def workflow_new_page(request: Request) -> HTMLResponse:
    """A blank canvas."""
    return _editor(request, None)


@router.get(
    "/workflows/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def workflow_edit_page(request: Request, name: str, draft: int = 0) -> HTMLResponse:
    """The canvas editor for one workflow."""
    return _editor(request, name, draft=bool(draft))


@router.get(
    "/workflows/{name}/runs/{run_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def workflow_run_page(request: Request, name: str, run_id: str) -> HTMLResponse:
    """A live, read-only canvas of one run."""
    run = workflow_runner_svc.get_run(run_id)
    if run is None or run.workflow != name:
        raise HTTPException(status_code=404, detail=f"Workflow run '{run_id}' not found.")
    response = templates.TemplateResponse(
        request,
        "workflow_run.html",
        {
            "name": name,
            "run": run.model_dump(by_alias=True),
            "catalog": _editor_catalog(),
            "can_run": _can(request, "submit_jobs"),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _back(flash: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in (("flash", flash), ("error", error)) if v})
    return RedirectResponse(url="/workflows" + (f"?{query}" if query else ""), status_code=303)


@router.post(
    "/workflows/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def workflow_delete_post(name: str) -> RedirectResponse:
    """Delete a workflow (a human action from the index)."""
    try:
        workflows_svc.delete_workflow(name, actor="web")
    except workflows_svc.WorkflowError as exc:
        return _back(error=str(exc))
    return _back(flash=f"Workflow '{name}' deleted.")


@router.post(
    "/workflows/{name}/duplicate",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def workflow_duplicate_post(name: str, new_name: str = Form("")) -> RedirectResponse:
    """Duplicate a workflow and open the copy on the canvas."""
    try:
        result = workflows_svc.duplicate_workflow(name, new_name or None, actor="web")
    except workflows_svc.WorkflowError as exc:
        return _back(error=str(exc))
    return RedirectResponse(url=f"/workflows/{result.name}", status_code=303)


@router.post(
    "/workflows/{name}/run",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def workflow_run_post(name: str) -> RedirectResponse:
    """Start a run and open its live view."""
    detail = workflows_svc.get_workflow(name)
    if detail is None:
        return _back(error=f"Workflow '{name}' not found.")
    try:
        run_id = workflow_runner_svc.start_run(detail, trigger="manual", actor="web")
    except workflow_runner_svc.WorkflowRunError as exc:
        return _back(error=str(exc))
    return RedirectResponse(url=f"/workflows/{name}/runs/{run_id}", status_code=303)


@router.post(
    "/workflows/{name}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def workflow_toggle_post(name: str, enabled: str = Form("0")) -> RedirectResponse:
    """Enable / disable a workflow's trigger."""
    on = enabled in ("1", "true", "on")
    try:
        workflows_svc.set_enabled(name, on, actor="web")
    except workflows_svc.WorkflowError as exc:
        return _back(error=str(exc))
    return _back(flash=f"Workflow '{name}' {'enabled' if on else 'disabled'}.")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Routes module; mounted by ovid2.web.app.")
