"""JSON API of the Ovid Atlas (``/navigator``) — ``/api/v1/atlas/*``.

Read routes need ``VIEW_PIPELINE_DATA`` (the same as the navigator and the
Data Space). Saving a shared view needs ``MANAGE_CONFIG``; presence is a
per-tab heartbeat any viewer sends. Every action that *changes* the system
(run / pause / cancel a job, save a spec, edit an address, re-bind an
engine) goes through the routes that already own it — the Atlas adds no
second write path. ``rebind:check`` is a dry-run only.

All JSON writes declare :func:`require_json_content_type` (the API's CSRF
defence). ADR: the adr-atlas design note.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ovid2.web.auth.csrf import require_json_content_type
from ovid2.web.auth.deps import AuthContext, require_capability
from ovid2.web.auth.roles import Capability
from ovid2.web.routes._route_policy import owner_scoped
from ovid2.web.services import atlas_state_svc, atlas_svc

router = APIRouter(prefix="/api/v1/atlas", tags=["atlas"])


class AtlasLayoutWrite(BaseModel):
    """Body of ``PUT /atlas/layouts/{name}``: one shared saved view."""

    layout: dict[str, Any] = Field(
        ...,
        description=(
            "View state: any of layers, expanded, pinned, mode, offsets, dock, "
            "overlays, focus, zoom."
        ),
    )


class AtlasPresenceBeat(BaseModel):
    """Body of ``POST /atlas/presence``: one tab's heartbeat."""

    client_id: str = Field(..., description="Random per-tab id (6-64 chars, [A-Za-z0-9_-]).")
    node: str | None = Field(None, description="Atlas node id the tab is focused on.")
    mode: str = Field("view", description="`view`, or `edit` while an editor is open (soft lock).")


class AtlasRebindCheck(BaseModel):
    """Body of ``POST /atlas/rebind:check`` (dry run; nothing is saved)."""

    config: str = Field(..., description="Saved pipeline config name.")
    role: str = Field(..., description="`engine`, `input` or `output`.")
    target: str = Field(..., description="Engine name / address name to bind.")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get(
    "",
    summary="The workspace's Atlas graph: storage, compute and algorithm layers",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_graph(response: Response) -> dict[str, Any]:
    """Return ``{workspace, layers, nodes, edges, digest}`` (static; no data read)."""
    _no_store(response)
    return atlas_svc.build_graph()


@router.get(
    "/nodes/{node_id:path}",
    summary="Expand one Atlas node (lineage, saved config, address, engine, workflow)",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_node(node_id: str, response: Response) -> dict[str, Any]:
    """Return the in-place detail of one node; 404 for an unknown node."""
    _no_store(response)
    try:
        return atlas_svc.node_detail(node_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get(
    "/live",
    summary="Live state: running / queued / just-finished jobs with table and stage timings",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_atlas_live(response: Response) -> dict[str, Any]:
    """Return ``{server_now_ms, jobs, workflow_runs}`` for the live animation."""
    _no_store(response)
    return atlas_svc.live_state()


@router.get(
    "/replay/{instance_id}",
    summary="Saved per-table / per-stage timeline of a finished run (canvas replay)",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_atlas_replay(instance_id: str) -> dict[str, Any]:
    """Return ``{job, items}`` from ``web_run_items`` (survives the event log's loss)."""
    from ovid2.web.services import jobs_svc, run_items_svc

    job = jobs_svc.get_job_by_instance(instance_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Run '{instance_id}' not found.")
    items = run_items_svc.times_for_page(
        instance_id,
        live=job.status in ("queued", "running"),
        job_status=job.status,
        ended_at=job.ended_at,
    )
    return {"job": atlas_svc.job_brief(job), "items": items}


@router.get(
    "/overlays/heat",
    summary="Heat overlay: historical duration and failure rate per config / table / stage",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_atlas_heat(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Aggregates only (numbers)."""
    return atlas_svc.heat(days=days)


@router.get(
    "/overlays/health",
    summary="Health overlay: latest failed data-health checks per address and table",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_health() -> dict[str, Any]:
    """Aggregates only; never row values."""
    return atlas_svc.health_overlay()


@router.get(
    "/overlays/drift",
    summary="Drift overlay: configs whose definition changed since their last successful run",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_drift() -> dict[str, Any]:
    """File modification times only."""
    return atlas_svc.config_drift()


@router.get(
    "/impact",
    summary="Impact preview: everything downstream of a node (or a field)",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_impact(
    node: str = Query(..., description="Node id, or `field:<package>/<table>/<FIELD>`."),
) -> dict[str, Any]:
    """Return ``{root, nodes, edges, fields?}``."""
    try:
        return atlas_svc.impact(node)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get(
    "/subgraph",
    summary="Row-free sub-graph around selected nodes (the assistant's context)",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_subgraph(
    ids: str = Query(..., description="Comma-separated node ids."),
    depth: int = Query(1, ge=0, le=2),
) -> dict[str, Any]:
    """Node metadata and links only — no secret values, no row values."""
    return atlas_svc.subgraph([i for i in ids.split(",") if i.strip()][:50], depth=depth)


@router.post(
    "/rebind:check",
    summary="Dry-run a drag-to-rebind (engine / input / output) against the capability plan",
    response_model=dict[str, Any],
    dependencies=[
        Depends(require_capability(Capability.VIEW_PIPELINE_DATA)),
        Depends(require_json_content_type),
    ],
)
def api_atlas_rebind_check(body: AtlasRebindCheck) -> dict[str, Any]:
    """Return ``{ok, reason, patch}``; the browser saves ``patch`` via PUT /pipelines."""
    try:
        return atlas_svc.check_rebind(body.config, role=body.role, target=body.target)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/forms/{kind}/{type_name}",
    summary="Field catalogue of one connection / location / engine type (for in-place edit)",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_form(kind: str, type_name: str) -> dict[str, Any]:
    """Return ``{args, secrets}`` field specs (``widget`` / ``choices`` included, UI rule U2).

    ``kind`` is ``connection``, ``location`` or ``engine``. Secret *fields*
    are described (names, help) but never their values.
    """
    try:
        if kind == "connection":
            from ovid2.connections.registry import connection_field_spec

            spec = connection_field_spec(type_name)
            return {"args": spec["args"], "secrets": spec["secrets"]}
        if kind == "location":
            from ovid2.connections.registry import location_field_spec

            return {"args": location_field_spec(type_name), "secrets": []}
        if kind == "engine":
            from ovid2.config.engines import engine_field_spec

            spec = engine_field_spec(type_name)
            return {"args": spec["args"], "secrets": spec["secrets"]}
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    raise HTTPException(status_code=404, detail=f"Unknown form kind '{kind}'.")


@router.get(
    "/layouts",
    summary="Shared saved views of the workspace's Atlas",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_layouts() -> list[dict[str, Any]]:
    """Every member of the workspace sees the same views."""
    return atlas_state_svc.list_layouts()


@router.put(
    "/layouts/{name}",
    summary="Save a shared Atlas view",
    response_model=dict[str, Any],
    dependencies=[
        Depends(require_capability(Capability.MANAGE_CONFIG)),
        Depends(require_json_content_type),
    ],
)
def api_atlas_save_layout(
    name: str,
    body: AtlasLayoutWrite,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_CONFIG)),
) -> dict[str, Any]:
    """Create or replace a view (layer toggles, expanded / pinned nodes, offsets)."""
    try:
        return atlas_state_svc.save_layout(name, body.layout, actor=ctx.email or ctx.user_id)
    except atlas_state_svc.AtlasStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete(
    "/layouts/{name}",
    status_code=204,
    summary="Delete a saved Atlas view you created",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
    openapi_extra=owner_scoped(
        "Deletes only a saved canvas view the caller created; no data or config."
    ),
)
def api_atlas_delete_layout(
    name: str,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_CONFIG)),
) -> Response:
    """404 when absent, 403 when another member created it."""
    try:
        found = atlas_state_svc.delete_layout(name, actor=ctx.email or ctx.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    if not found:
        raise HTTPException(status_code=404, detail=f"View '{name}' not found.")
    return Response(status_code=204)


@router.post(
    "/presence",
    summary="Heartbeat this tab's Atlas presence; returns everyone else's",
    response_model=list[dict[str, Any]],
    dependencies=[
        Depends(require_capability(Capability.VIEW_PIPELINE_DATA)),
        Depends(require_json_content_type),
    ],
)
def api_atlas_presence(
    body: AtlasPresenceBeat,
    ctx: AuthContext = Depends(require_capability(Capability.VIEW_PIPELINE_DATA)),
) -> list[dict[str, Any]]:
    """An ``edit`` beat is a soft lock other members see; nothing is blocked."""
    try:
        return atlas_state_svc.heartbeat(
            user_id=ctx.user_id,
            display_name=ctx.email or ctx.user_id,
            client_id=body.client_id,
            node=body.node,
            mode=body.mode,
        )
    except atlas_state_svc.AtlasStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/presence",
    summary="Who else is viewing / editing which Atlas node",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_atlas_presence_list() -> list[dict[str, Any]]:
    """Live presence rows of the active workspace."""
    return atlas_state_svc.presence()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.web.routes.atlas_api is imported by the web app.")
