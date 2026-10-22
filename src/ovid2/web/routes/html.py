"""HTML pages — dashboard, health, addresses, runs.

Each route renders a Jinja2 template extending ``base.html``. HTMX
``hx-get`` targets return smaller partials (``_*.html``) so the browser
can swap only what changed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ovid2.config import addresses as _addresses
from ovid2.web.auth.csrf import require_csrf
from ovid2.web.auth.deps import require_capability
from ovid2.web.auth.roles import Capability, Role

# TODO(csrf): wire require_csrf + {{ csrf_token() }} into the remaining
# mutation forms (addresses, connections, engines, routes/pipelines,
# formatters, mappers, submit, install). Wave-3 todo 15 wires only the
# settings + secrets forms as the proof-of-concept to keep parallel-edit
# risk low; the rest are a follow-up pass.
from ovid2.web.models import (
    AddressWrite,
    ConnectionWrite,
    EngineWrite,
    FormatterWrite,
    GlobalSettings,
    JobSubmitRequest,
    MapperWrite,
    PipelineTrigger,
    PipelineWrite,
    ScaffoldPipelineRequest,
    SecretWrite,
    SettingsAdmin,
    SettingsDefaults,
    SettingsNotifications,
    SettingsRetention,
    SettingsUi,
    SettingsWorker,
    normalize_kind,
)
from ovid2.web.services import (
    addresses_svc,
    cdms_svc,
    connections_svc,
    dataspace_svc,
    engines_svc,
    events_svc,
    formatters_svc,
    fs_svc,
    health_report_svc,
    health_svc,
    install_svc,
    jobs_svc,
    mappers_svc,
    pipelines_meta_svc,
    pipelines_svc,
    runs_svc,
    secrets_svc,
    settings_svc,
    templates_svc,
    timeline_svc,
    users_svc,
)
from ovid2.web.settings import DASHBOARD_POLL_MS, RUN_EVENTS_POLL_MS
from ovid2.web.templating import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Console landing (``/``) + Dashboard (``/dashboard``)
#
# ``/`` is the terminal-styled console: the same bird's-eye data as the
# dashboard (health / cooking / runs / queue / KPIs) plus a client-side
# command palette. The classic card-grid dashboard is preserved verbatim
# at ``/dashboard``; only its top-level route path and the nav link
# target moved (see ``base.html``). Every route here stays
# ``include_in_schema=False`` so the OpenAPI snapshot is unaffected.
# ---------------------------------------------------------------------------


# Static navigable targets for the command palette. Kept module-level so
# the shape is obvious and the ``console`` handler stays thin. Each entry
# is ``{"label", "url", "kind": "page", "keywords"}``; dynamic ``run``
# entries are appended per-request from the run tracker.
_PALETTE_PAGES: tuple[dict[str, str], ...] = (
    {
        "label": "Submit run",
        "url": "/submit",
        "kind": "page",
        "keywords": "submit new run job",
        "capability": "submit_jobs",
    },
    {
        "label": "Pipelines",
        "url": "/pipelines",
        "kind": "page",
        "keywords": "pipelines routes",
        "capability": "view_pipeline_data",
    },
    {
        "label": "Runs",
        "url": "/runs",
        "kind": "page",
        "keywords": "runs jobs history batches queue",
        "capability": "view_runs",
    },
    {
        "label": "Data Space",
        "url": "/dataspace",
        "kind": "page",
        "keywords": "dataspace data space",
        "capability": "view_pipeline_data",
    },
    {
        "label": "Storage accounts",
        "url": "/storage",
        "kind": "page",
        "keywords": "storage accounts connections addresses",
        "capability": "view_pipeline_data",
    },
    {
        "label": "Warehouses",
        "url": "/warehouses",
        "kind": "page",
        "keywords": "warehouses engines",
        "capability": "view_pipeline_data",
    },
    {
        "label": "Secrets",
        "url": "/secrets",
        "kind": "page",
        "keywords": "secrets credentials",
        "capability": "manage_secrets",
    },
    {
        "label": "Users",
        "url": "/users",
        "kind": "page",
        "keywords": "users access roles admin",
        "capability": "manage_users",
    },
    {
        "label": "CDMs",
        "url": "/cdms",
        "kind": "page",
        "keywords": "cdm common data model",
        "capability": "view_public",
    },
    {
        "label": "Settings",
        "url": "/settings",
        "kind": "page",
        "keywords": "settings preferences",
        "capability": "admin_all",
    },
    {
        "label": "Setup wizard",
        "url": "/install",
        "kind": "page",
        "keywords": "install setup wizard",
        "capability": "admin_all",
    },
    {
        "label": "Health",
        "url": "/health",
        "kind": "page",
        "keywords": "health doctor diagnostics",
        "capability": "view_public",
    },
    {
        "label": "Dashboard",
        "url": "/dashboard",
        "kind": "page",
        "keywords": "dashboard cards overview",
        "capability": "view_public",
    },
    {
        "label": "API docs",
        "url": "/docs",
        "kind": "page",
        "keywords": "api docs openapi swagger",
        "capability": "view_public",
    },
)


def _build_palette_targets(request: Request, runs: object) -> list[dict[str, str]]:
    """Assemble the command-palette target list, filtered by capability.

    Combines the static page roster (:data:`_PALETTE_PAGES`) with one
    ``kind == "run"`` entry per recent run so operators can jump
    straight to a run's detail page by typing its batch id or pipeline.
    Built only from the already-fetched ``runs`` response — no extra
    service call and no pipeline import.

    Each page entry carries a ``capability`` the current role must hold;
    targets the role lacks are omitted (UX parity with the nav — the
    routes themselves are gated separately in todos 13/14). When auth is
    disabled the synthetic admin holds every capability, so every target
    is emitted (today's behaviour). Run entries require ``view_runs``.

    Returns a JSON-serialisable ``list[dict[str, str]]`` (the emitted
    dicts carry only ``label`` / ``url`` / ``kind`` / ``keywords`` — the
    internal ``capability`` key is stripped).
    """
    from ovid2.web.templating import has_cap

    targets: list[dict[str, str]] = []
    for page in _PALETTE_PAGES:
        capability = page.get("capability", "view_public")
        if not has_cap(request, capability):
            continue
        targets.append(
            {k: v for k, v in page.items() if k != "capability"}
        )
    if not has_cap(request, "view_runs"):
        return targets
    run_rows = getattr(runs, "runs", None) or []
    for run in run_rows:
        batch_id = getattr(run, "batch_id", None)
        if not batch_id:
            continue
        instance_id = getattr(run, "instance_id", None)
        pipeline = getattr(run, "pipeline", None) or ""
        targets.append(
            {
                "label": f"run {batch_id}",
                "url": f"/runs/{instance_id or batch_id}",
                "kind": "run",
                "keywords": f"{pipeline} {batch_id}".strip(),
            }
        )
    return targets


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def console(request: Request) -> HTMLResponse:
    """Render the terminal-styled console landing page.

    Serves the same bird's-eye context as the dashboard plus a
    server-built ``palette_targets`` list for the client-side command
    palette. Panels refresh via HTMX polling ``/partials/console``.
    """
    from ovid2.web.services import metrics_svc

    health = health_svc.collect_health()
    runs = runs_svc.list_runs(limit=10, offset=0)
    jobs = jobs_svc.list_jobs(limit=10, offset=0)
    cooking = metrics_svc.briefing_cooking()
    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "health": health,
            "runs": runs,
            "jobs": jobs,
            "cooking": cooking,
            "poll_ms": DASHBOARD_POLL_MS,
            "palette_targets": _build_palette_targets(request, runs),
        },
    )


@router.get("/partials/console", response_class=HTMLResponse, include_in_schema=False)
def console_partial(request: Request) -> HTMLResponse:
    """HTMX polling target — refreshed console bird's-eye panels."""
    from ovid2.web.services import metrics_svc

    health = health_svc.collect_health()
    runs = runs_svc.list_runs(limit=10, offset=0)
    jobs = jobs_svc.list_jobs(limit=10, offset=0)
    cooking = metrics_svc.briefing_cooking()
    return templates.TemplateResponse(
        request,
        "_console_body.html",
        {"health": health, "runs": runs, "jobs": jobs, "cooking": cooking},
    )


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request) -> HTMLResponse:
    """Render the classic card-grid dashboard (moved from ``/``)."""
    from ovid2.web.services import metrics_svc

    health = health_svc.collect_health()
    runs = runs_svc.list_runs(limit=10, offset=0)
    jobs = jobs_svc.list_jobs(limit=10, offset=0)
    cooking = metrics_svc.briefing_cooking()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "health": health,
            "runs": runs,
            "jobs": jobs,
            "cooking": cooking,
            "poll_ms": DASHBOARD_POLL_MS,
        },
    )


@router.get("/partials/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard_partial(request: Request) -> HTMLResponse:
    """HTMX polling target — refreshed table body + health badges."""
    from ovid2.web.services import metrics_svc

    health = health_svc.collect_health()
    runs = runs_svc.list_runs(limit=10, offset=0)
    jobs = jobs_svc.list_jobs(limit=10, offset=0)
    cooking = metrics_svc.briefing_cooking()
    return templates.TemplateResponse(
        request,
        "_dashboard_body.html",
        {"health": health, "runs": runs, "jobs": jobs, "cooking": cooking},
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", response_class=HTMLResponse, include_in_schema=False)
def health_page(request: Request) -> HTMLResponse:
    """Render the doctor report page."""
    return templates.TemplateResponse(
        request, "health.html", {"health": health_svc.collect_health()}
    )


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


@router.get("/addresses", response_class=HTMLResponse, include_in_schema=False)
def addresses_page(request: Request) -> RedirectResponse:
    """Legacy URL — addresses are folded into storage accounts.

    Per-run location (bucket / path / schema) is chosen on the pipeline
    at submit time rather than as a standalone resource. Existing
    address configs still load; this list view now redirects to the
    storage-account index.
    """
    return RedirectResponse(url="/storage", status_code=307)


# ---------------------------------------------------------------------------
# Addresses — CRUD (W4, connection-first UI)
#
# Route ordering matters: FastAPI matches routes in insertion order and
# ``/addresses/new`` would collide with ``/addresses/{name}`` unless
# declared first.
#
# The create flow is two-step:
#
#   1. GET  /addresses/new         → pick a connection.
#   2. GET  /addresses/new/step2   → fill a location for that connection.
#   3. POST /addresses             → persist.
#
# Edit flow branches by shape:
#
#   * W7 rows (``{connection, location}``) render ``address_edit_connection.html``.
#   * Legacy rows (``{src_name, src_args}``) still use the original
#     ``address_edit.html`` and show a "convert" banner.
# ---------------------------------------------------------------------------


_ADDRESS_KNOWN_KEYS: dict[str, tuple[str, ...]] = {
    "disc": ("path",),
    "snowflake": (
        "account",
        "user",
        "password",
        "warehouse",
        "database",
        "schema",
        "role",
        "private_key",
        "private_key_passphrase",
    ),
}


def _get_location_spec(conn_type: str) -> list[dict[str, object]]:
    """Return the location field descriptors for a connection type.

    Wraps :func:`ovid2.connections.registry.location_field_spec` in a
    defensive ``try`` so an unknown type (e.g. a corrupted connection
    file) surfaces as an empty form rather than a 500.
    """
    from ovid2.connections.registry import location_field_spec
    try:
        return location_field_spec(conn_type)
    except KeyError:
        return []


@router.get("/addresses/new", response_class=HTMLResponse, include_in_schema=False)
def address_new_pick_connection(
    request: Request,
    error: str | None = None,
) -> HTMLResponse:
    """Step 1 — pick the backing connection.

    Renders every existing connection as a table row with a *Choose*
    button that links to :func:`address_new_step2`. When no
    connections are configured yet, points the operator at
    ``/connections/new``.
    """
    return templates.TemplateResponse(
        request,
        "address_new_pick_connection.html",
        {
            "connections": connections_svc.list_connections(),
            "error": error,
        },
    )


@router.get(
    "/addresses/new/step2",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def address_new_step2(
    request: Request,
    connection: str,
    error: str | None = None,
) -> HTMLResponse:
    """Step 2 — fill the location fields for the chosen connection.

    Args:
        connection: Filename stem of an existing connection.
        error: Optional error text carried across a POST redirect.
    """
    detail = connections_svc.get_connection(connection)
    if detail is None:
        # Bounce back to step 1 with a helpful flash.
        return RedirectResponse(
            url=f"/addresses/new?error=Connection+%27{connection}%27+not+found",
            status_code=303,
        )
    location_spec = _get_location_spec(detail.type)
    return templates.TemplateResponse(
        request,
        "address_new_form.html",
        {
            "mode": "new",
            "name": "",
            "connection": detail,
            "location_spec": location_spec,
            "location": {},
            "error": error,
        },
    )


def _parse_location_form(
    form_body: dict[str, str],
    location_spec: list[dict[str, object]],
) -> dict[str, object]:
    """Extract typed location values from the flat form payload.

    Uses the spec to coerce booleans (checkboxes) and skip blanks so
    the on-disk JSON stays compact — a blank ``schema`` becomes an
    absent key rather than ``"schema": ""``.
    """
    values: dict[str, object] = {}
    for field in location_spec:
        key = str(field["name"])
        form_key = f"loc_{key}"
        raw = form_body.get(form_key, "")
        field_type = str(field.get("type", "str"))
        if field_type == "bool":
            # Unchecked checkboxes are absent from the form entirely.
            # A present key with value "on" (browser default) or a
            # truthy string means checked.
            if form_key in form_body:
                values[key] = str(raw).lower() in {"on", "true", "1", "yes"}
            else:
                values[key] = False
        elif field_type == "int":
            raw_stripped = str(raw).strip()
            if raw_stripped:
                try:
                    values[key] = int(raw_stripped)
                except ValueError:
                    values[key] = raw_stripped  # let the service raise if it cares
        else:
            raw_stripped = str(raw).strip()
            if raw_stripped:
                values[key] = raw_stripped
    return values


def _parse_kv_form(form_body: dict[str, str]) -> dict[str, str]:
    """Parse a form of paired ``arg_key_N`` / ``arg_val_N`` fields.

    Retained for the legacy edit form, which continues to render the
    free-form key/value grid for pre-W7 configs.
    """
    pairs: dict[str, str] = {}
    for key, value in form_body.items():
        if not key.startswith("arg_key_"):
            continue
        idx = key.removeprefix("arg_key_")
        val = form_body.get(f"arg_val_{idx}", "")
        key_name = value.strip()
        if not key_name:
            continue
        pairs[key_name] = val
    return pairs


@router.post("/addresses", response_class=HTMLResponse, include_in_schema=False)
async def address_create_post(request: Request) -> RedirectResponse:
    """Handle the step-2 "new address" form submission.

    Only accepts the W7 shape (``{connection, location}``) — legacy
    creates are refused server-side (see
    :func:`addresses_svc.write_address`). On validation error we
    bounce back to step 2 so the operator does not lose their inputs.
    """
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    connection_name = str(form.get("connection", "")).strip()

    detail = connections_svc.get_connection(connection_name) if connection_name else None
    if detail is None:
        return RedirectResponse(
            url="/addresses/new?error=Pick+a+connection+first", status_code=303,
        )
    location_spec = _get_location_spec(detail.type)
    location = _parse_location_form({k: str(v) for k, v in form.items()}, location_spec)

    try:
        payload = AddressWrite(connection=connection_name, location=location)
        addresses_svc.write_address(name, payload, actor="web", create_only=True)
    except (addresses_svc.AddressError, ValueError) as exc:
        return RedirectResponse(
            url=(
                f"/addresses/new/step2?connection={connection_name}"
                f"&error={exc}"
            ),
            status_code=303,
        )
    return RedirectResponse(url=f"/addresses/{name}", status_code=303)


@router.get(
    "/addresses/{name}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def address_edit_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the edit form for an existing address.

    Branches on shape: W7 rows use the location-only form; legacy
    rows retain the free-form key/value grid so their credentials
    stay editable through the web tier.
    """
    detail = addresses_svc.get_address(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Address '{name}' not found.")

    if addresses_svc.is_legacy_shape(detail):
        return templates.TemplateResponse(
            request,
            "address_edit.html",
            {
                "mode": "edit",
                "name": name,
                "src_name": detail.src_name,
                "src_args": detail.src_args,
                "known_keys": _ADDRESS_KNOWN_KEYS,
                "error": error,
            },
        )

    # W7 shape: render the location-only form.
    assert detail.connection is not None  # narrowed by is_legacy_shape check
    conn_detail = connections_svc.get_connection(detail.connection)
    location_spec = (
        _get_location_spec(conn_detail.type) if conn_detail is not None else []
    )
    return templates.TemplateResponse(
        request,
        "address_new_form.html",
        {
            "mode": "edit",
            "name": name,
            "connection": conn_detail,
            "location_spec": location_spec,
            "location": detail.location or {},
            "error": error,
        },
    )


@router.post(
    "/addresses/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def address_delete_post(request: Request, name: str) -> RedirectResponse:
    """Delete an address config and return to the index."""
    try:
        addresses_svc.delete_address(name, actor="web")
    except addresses_svc.AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/addresses", status_code=303)


@router.get("/addresses/{name}", response_class=HTMLResponse, include_in_schema=False)
def address_detail_page(request: Request, name: str) -> HTMLResponse:
    """Render one address config with masked secrets."""
    detail = addresses_svc.get_address(name)
    return templates.TemplateResponse(
        request,
        "address_detail.html",
        {
            "address": detail,
            "name": name,
            "is_legacy": addresses_svc.is_legacy_shape(detail),
        },
    )


@router.post(
    "/addresses/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def address_update_post(request: Request, name: str) -> RedirectResponse:
    """Handle the address edit form submission (PUT semantics).

    Accepts both shapes so legacy rows stay editable. The connection-
    shape branch uses the ``loc_*`` field naming scheme emitted by
    :file:`address_new_form.html`; the legacy branch continues to
    read the paired ``arg_key_N`` / ``arg_val_N`` grid.
    """
    form = dict(await request.form())
    form_str = {k: str(v) for k, v in form.items()}
    connection_name = form_str.get("connection", "").strip()

    if connection_name:
        detail = connections_svc.get_connection(connection_name)
        if detail is None:
            return RedirectResponse(
                url=f"/addresses/{name}/edit?error=Connection+not+found",
                status_code=303,
            )
        location_spec = _get_location_spec(detail.type)
        location = _parse_location_form(form_str, location_spec)
        payload = AddressWrite(connection=connection_name, location=location)
    else:
        src_name = form_str.get("src_name", "disc").strip()
        src_args = _parse_kv_form(form_str)
        payload = AddressWrite(src_name=src_name, src_args=src_args)

    try:
        addresses_svc.write_address(name, payload, actor="web")
    except (addresses_svc.AddressError, ValueError) as exc:
        return RedirectResponse(
            url=f"/addresses/{name}/edit?error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/addresses/{name}", status_code=303)


# ---------------------------------------------------------------------------
# Secrets (W4)
#
# Same route-ordering rule as addresses: static paths precede parameterised.
# ---------------------------------------------------------------------------


@router.get("/secrets", response_class=HTMLResponse, include_in_schema=False)
def secrets_index(request: Request) -> HTMLResponse:
    """List every secret file under the user-scoped ``secrets.d/``."""
    return templates.TemplateResponse(
        request,
        "secrets.html",
        {"secrets": secrets_svc.list_secrets()},
    )


@router.get("/secrets/new", response_class=HTMLResponse, include_in_schema=False)
def secret_new_page(
    request: Request,
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new secret" form."""
    return templates.TemplateResponse(
        request,
        "secret_edit.html",
        {
            "mode": "new",
            "name": "",
            "detail": None,
            "error": error,
        },
    )


@router.post("/secrets", response_class=HTMLResponse, include_in_schema=False)
async def secret_create_post(
    request: Request, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Handle the "new secret" form."""
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    values = _parse_kv_form({k: str(v) for k, v in form.items()})
    try:
        payload = SecretWrite(values=values)
        secrets_svc.write_secret(name, payload, actor="web", create_only=True)
    except (secrets_svc.SecretError, ValueError) as exc:
        return RedirectResponse(url=f"/secrets/new?error={exc}", status_code=303)
    return RedirectResponse(url="/secrets", status_code=303)


@router.get(
    "/secrets/{name}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def secret_edit_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the edit form for an existing secret (keys only — no values)."""
    detail = secrets_svc.get_secret(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found.")
    return templates.TemplateResponse(
        request,
        "secret_edit.html",
        {
            "mode": "edit",
            "name": name,
            "detail": detail,
            "error": error,
        },
    )


@router.post(
    "/secrets/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def secret_delete_post(
    request: Request, name: str, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Delete a secret file and return to the index."""
    try:
        secrets_svc.delete_secret(name, actor="web")
    except secrets_svc.SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/secrets", status_code=303)


@router.post(
    "/secrets/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def secret_update_post(
    request: Request, name: str, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Handle the secret edit form (PUT semantics)."""
    form = dict(await request.form())
    values = _parse_kv_form({k: str(v) for k, v in form.items()})
    try:
        payload = SecretWrite(values=values)
        secrets_svc.write_secret(name, payload, actor="web")
    except (secrets_svc.SecretError, ValueError) as exc:
        return RedirectResponse(
            url=f"/secrets/{name}/edit?error={exc}", status_code=303
        )
    return RedirectResponse(url="/secrets", status_code=303)


# ---------------------------------------------------------------------------
# Users (W6) — admin user management (todo 18)
#
# Admin-only page listing every ``web_users`` row with activate / set-role /
# disable / enable actions. The read route is gated with
# ``require_capability(MANAGE_USERS)`` (security never relies on nav hiding);
# every mutation form carries the CSRF token and its POST route depends on
# ``require_csrf``. Only email / name / role / status / last_login are shown —
# never session ids, issuer / subject internals, or break-glass hashes.
# ---------------------------------------------------------------------------

#: Roles an admin may assign through the UI (``pending`` is never assignable).
_ASSIGNABLE_ROLES: tuple[Role, ...] = (
    Role.SYSTEM_ADMIN,
    Role.PIPELINE_OPERATOR,
    Role.VIEWER,
    Role.GUEST,
)


def _parse_assignable_role(raw: str) -> Role:
    """Parse a submitted role string into an assignable :class:`Role`.

    Args:
        raw: The raw ``role`` form value.

    Returns:
        The resolved role.

    Raises:
        users_svc.UserError: If ``raw`` is not a recognised role or resolves
            to :data:`~ovid2.web.auth.roles.Role.PENDING` (never assignable).
    """
    try:
        role = Role(raw.strip())
    except ValueError as exc:
        raise users_svc.UserError(f"Invalid role: {raw!r}") from exc
    if role not in _ASSIGNABLE_ROLES:
        raise users_svc.UserError(f"Role not assignable: {role.value}")
    return role


@router.get(
    "/users",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def users_index(request: Request, error: str | None = None) -> HTMLResponse:
    """List every web user with a pending-approvals highlight (admin-only).

    Args:
        request: The inbound request.
        error: Optional error message surfaced via the redirect query param.

    Returns:
        The rendered ``users.html`` page.
    """
    users = users_svc.list_users()
    pending = [u for u in users if u["status"] == "pending"]
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users,
            "pending": pending,
            "roles": [r.value for r in _ASSIGNABLE_ROLES],
            "error": error,
        },
    )


@router.post(
    "/users/{user_id}/activate",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def user_activate_post(
    request: Request,
    user_id: str,
    role: str = Form(...),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Activate a pending user with the submitted role.

    Args:
        request: The inbound request.
        user_id: The target user's identifier.
        role: The role to assign (form field).
        _csrf: CSRF guard dependency.

    Returns:
        A 303 redirect back to ``/users`` (with ``?error=`` on failure).
    """
    try:
        users_svc.activate(user_id, _parse_assignable_role(role), actor="web")
    except users_svc.UserError as exc:
        return RedirectResponse(url=f"/users?error={exc}", status_code=303)
    return RedirectResponse(url="/users", status_code=303)


@router.post(
    "/users/{user_id}/role",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def user_role_post(
    request: Request,
    user_id: str,
    role: str = Form(...),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Change a user's role.

    Args:
        request: The inbound request.
        user_id: The target user's identifier.
        role: The new role (form field).
        _csrf: CSRF guard dependency.

    Returns:
        A 303 redirect back to ``/users`` (with ``?error=`` on failure).
    """
    try:
        users_svc.set_role(user_id, _parse_assignable_role(role), actor="web")
    except users_svc.UserError as exc:
        return RedirectResponse(url=f"/users?error={exc}", status_code=303)
    return RedirectResponse(url="/users", status_code=303)


@router.post(
    "/users/{user_id}/disable",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def user_disable_post(
    request: Request,
    user_id: str,
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Disable a user (last-admin guarded).

    Args:
        request: The inbound request.
        user_id: The target user's identifier.
        _csrf: CSRF guard dependency.

    Returns:
        A 303 redirect back to ``/users`` (with ``?error=`` on failure).
    """
    try:
        users_svc.disable(user_id, actor="web")
    except users_svc.UserError as exc:
        return RedirectResponse(url=f"/users?error={exc}", status_code=303)
    return RedirectResponse(url="/users", status_code=303)


@router.post(
    "/users/{user_id}/enable",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def user_enable_post(
    request: Request,
    user_id: str,
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Re-enable a disabled user.

    Args:
        request: The inbound request.
        user_id: The target user's identifier.
        _csrf: CSRF guard dependency.

    Returns:
        A 303 redirect back to ``/users`` (with ``?error=`` on failure).
    """
    try:
        users_svc.enable(user_id, actor="web")
    except users_svc.UserError as exc:
        return RedirectResponse(url=f"/users?error={exc}", status_code=303)
    return RedirectResponse(url="/users", status_code=303)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse, include_in_schema=False)
def runs_page(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    pipeline: str | None = None,
    selected: str | None = None,
) -> HTMLResponse:
    """Merged runs workspace: run-history list (left) + live console (right).

    Newest-first paginated list on the left; the right region shows the
    console for the ``selected`` run (an ``instance_id`` or ``batch_id``).
    When ``selected`` is omitted, the most-recent run is auto-selected so
    the workspace never opens with an empty canvas.
    """
    response = runs_svc.list_runs(
        limit=limit, offset=offset, status=status, pipeline=pipeline
    )
    # Auto-select the newest run when the caller did not pin one.
    if not selected and response.runs:
        top = response.runs[0]
        selected = top.instance_id or top.batch_id
    context: dict[str, object] = {
        "runs": response,
        "status": status,
        "pipeline": pipeline,
        "selected": selected,
    }
    if selected:
        context.update(_run_detail_context(selected))
    return templates.TemplateResponse(request, "runs.html", context)


@router.get(
    "/partials/runs/list", response_class=HTMLResponse, include_in_schema=False
)
def runs_list_partial(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    pipeline: str | None = None,
    selected: str | None = None,
) -> HTMLResponse:
    """Return one page of run-list rows for the infinite-scroll append.

    Rendered by the trailing sentinel in ``_runs_list_rows.html`` when it
    scrolls into view (``hx-trigger="revealed"``). Emits only the ``<li>``
    rows plus a fresh sentinel for the following page.
    """
    response = runs_svc.list_runs(
        limit=limit, offset=offset, status=status, pipeline=pipeline
    )
    return templates.TemplateResponse(
        request,
        "_runs_list_rows.html",
        {
            "runs": response,
            "status": status,
            "pipeline": pipeline,
            "selected": selected,
        },
    )


def _now_epoch_ms() -> int:
    """Server wall-clock in epoch milliseconds (UTC)."""
    from datetime import datetime, timezone

    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _str_list_or_none(value: object) -> list[str] | None:
    """Coerce an ``extra_args`` selection into a clean ``list[str]``.

    Returns ``None`` for absent / empty selections so the checks console
    falls back to the whole check catalogue rather than seeding an empty
    (and therefore always-queued) roster.
    """
    if not isinstance(value, (list, tuple)):
        return None
    out = [str(v) for v in value if str(v).strip()]
    return out or None


def _iso_to_epoch_ms(value: str | None) -> int | None:
    """Parse an ISO-8601 timestamp into epoch milliseconds.

    The web tier stores ``submitted_at`` / ``started_at`` / ``ended_at``
    as ISO-8601 UTC strings (see ``jobs_svc._now_iso``). The run-detail
    elapsed clock needs a numeric epoch anchor so it can resume across
    page refreshes rather than restarting from page-load time. Returns
    ``None`` for missing / unparseable input so the template can fall
    back to "not started yet".
    """
    if not value:
        return None
    from datetime import datetime, timezone

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _friendly_address(name: str | None) -> str | None:
    """Render an address config name as a human-readable ``connection/path``.

    The run-detail Input/Output fields store the *filename stem* of the
    address config (e.g. ``my-account__sandbox_ovid_19r2``),
    which is opaque to operators. When the config resolves to a W7
    ``{connection, location}`` shape we present it as a leading-slash
    ``/<connection>/<location-path>`` with the path lower-cased and its
    separators normalised to ``/`` (e.g.
    ``/my-account/sandbox/ovid_19r2``).

    Falls back to the raw ``name`` for legacy-shape configs, missing
    files, or any resolution error so the field never renders empty.

    Args:
        name: Address config filename stem, or ``None``.

    Returns:
        The friendly ``/connection/path`` string, the raw name on
        fallback, or ``None`` when ``name`` is falsy.
    """
    if not name:
        return name
    try:
        detail = addresses_svc.get_address(name)
    except Exception:
        return name
    if detail is None or not getattr(detail, "connection", None):
        return name
    location = getattr(detail, "location", None) or {}
    raw_path = location.get("path")
    if raw_path:
        path = str(raw_path).replace("\\", "/").strip("/").lower()
    else:
        # No explicit ``path`` — join whatever non-empty location values
        # exist (database / bucket / prefix / …) so the field is still
        # informative rather than bare.
        parts = [str(v).strip("/").lower() for v in location.values() if v]
        path = "/".join(parts)
    connection = str(detail.connection)
    return f"/{connection}/{path}" if path else f"/{connection}"


@router.get("/runs/{identifier}", response_class=HTMLResponse, include_in_schema=False)
def run_detail_page(
    request: Request,
    identifier: str,
    flash: str | None = None,
) -> HTMLResponse:
    """Render a run's console — dual-mode.

    ``identifier`` may be either a ``batch_id`` (from the run list) or an
    ``instance_id`` (event log).

    * A normal (full-page) GET renders the merged runs workspace with
      this run pre-selected, so bookmarks / deep links land on the full
      list + console layout.
    * A ``X-Requested-With: fetch`` GET (issued by ``live.js``'s in-place
      navigation) renders ONLY the run-console fragment so the detail
      region can be swapped without re-rendering the page shell.
    """
    context = _run_detail_context(identifier)
    # ``flash`` is intentionally no longer rendered on the run page — the
    # "Submitted job <id>" bar was redundant with the job-details box.
    # The param is still accepted so existing redirect URLs don't 404.
    context["flash"] = flash

    if request.headers.get("X-Requested-With") == "fetch":
        return templates.TemplateResponse(request, "run_detail.html", context)

    # Full page: render the merged workspace with this run selected.
    response = runs_svc.list_runs(limit=50, offset=0)
    context.update(
        {
            "runs": response,
            "status": None,
            "pipeline": None,
            "selected": identifier,
        }
    )
    return templates.TemplateResponse(request, "runs.html", context)


def _run_detail_context(identifier: str) -> dict[str, object]:
    """Build the shared run-console render context for ``identifier``.

    ``identifier`` may be a ``batch_id`` or an ``instance_id``. We resolve
    the run row when the former matches and always pass the identifier
    through as the event-stream cursor. When the identifier maps to a
    web-tier job, we also fetch that record so the console can render
    Cancel / Rerun controls, engine, addresses, and the elapsed anchors.
    """
    run = runs_svc.get_run(identifier)
    instance_id = run.instance_id if run and run.instance_id else identifier
    job = jobs_svc.get_job_by_instance(instance_id)
    # Engine lives on the nested ``extra_args`` payload, not as a
    # top-level JobDetail field (see jobs_svc.submit).
    engine = None
    if job and isinstance(job.extra_args, dict):
        engine = job.extra_args.get("engine")
    # Determine the run kind so the console can switch between the
    # per-table view (etl / analysis) and the per-check view
    # (health / dq_checks, which run the identical HealthCheckRunner and
    # emit check.* events). Default to ``etl`` for legacy jobs with no kind.
    run_kind = "etl"
    if job and isinstance(job.extra_args, dict):
        from ovid2.web.models import normalize_kind

        run_kind = normalize_kind(job.extra_args.get("kind"))
    # For health / dq_checks runs, surface the check *selection*
    # (parts / packages / checks, stored on ``extra_args`` by jobs_svc)
    # so the live console can pre-seed the full check roster before any
    # ``check.started`` event arrives — the checks-console analogue of
    # the tables console's ``data-planned-tables``. Absent / default
    # selections stay ``None`` and the browser falls back to the whole
    # catalogue.
    check_parts: list[str] | None = None
    check_packages: list[str] | None = None
    check_checks: list[str] | None = None
    if job and isinstance(job.extra_args, dict) and run_kind in ("health", "dq_checks"):
        ea = job.extra_args
        check_parts = _str_list_or_none(ea.get("parts"))
        check_packages = _str_list_or_none(ea.get("packages"))
        check_checks = _str_list_or_none(ea.get("checks"))
    # Whether a data-health report artifact exists for this run — drives
    # the "View report" link and the health console.
    report_available = health_report_svc.report_available(instance_id)
    # Anchor the elapsed clock to the real job start so a page refresh
    # resumes the timer instead of restarting from zero. Fall back to
    # the scheduler tracker's processing-start when the web job hasn't
    # recorded ``started_at`` yet.
    started_iso = None
    ended_iso = None
    if job:
        started_iso = job.started_at
        ended_iso = job.ended_at
    if not started_iso and run and getattr(run, "processing_start", None):
        started_iso = run.processing_start
    # The operator's display timezone. Server-rendered timestamps use
    # the Jinja ``localtime`` filters; the live console formats event
    # ``ts`` values client-side, so we hand the IANA zone name to the
    # browser via a data-attribute. Degrade to UTC so a bad setting can
    # never break the render.
    try:
        display_tz = settings_svc.get_settings().ui.display_timezone
    except Exception:
        display_tz = "UTC"
    return {
        "run": run,
        "job": job,
        "identifier": identifier,
        "instance_id": instance_id,
        "poll_ms": RUN_EVENTS_POLL_MS,
        "engine": engine,
        "input_address": _friendly_address(job.input_address) if job else None,
        "output_address": _friendly_address(job.output_address) if job else None,
        "started_at_ms": _iso_to_epoch_ms(started_iso),
        "ended_at_ms": _iso_to_epoch_ms(ended_iso),
        "server_now_ms": _now_epoch_ms(),
        "display_tz": display_tz,
        "run_kind": run_kind,
        "report_available": report_available,
        "check_parts": check_parts,
        "check_packages": check_packages,
        "check_checks": check_checks,
        "queue": jobs_svc.queue_position(job.job_id) if job else None,
    }


@router.get(
    "/runs/{instance_id}/report", response_class=HTMLResponse, include_in_schema=False
)
def run_report_page(request: Request, instance_id: str) -> HTMLResponse:
    """Render the clickable data-health report viewer for a run.

    Reads the JSON report artifact via :mod:`health_report_svc` (the
    sanctioned pipeline→web hand-off). Returns 404 when the run produced
    no report (e.g. it was an ETL run, or the health run hasn't finished
    writing the artifact yet).
    """
    report = health_report_svc.get_report(instance_id)
    if report is None:
        raise HTTPException(
            status_code=404, detail=f"No health report for run '{instance_id}'."
        )
    # Group checks by part for the nav tree.
    checks = report.get("checks", []) if isinstance(report, dict) else []
    parts: dict[str, list[dict]] = {}
    for check in checks:
        parts.setdefault(str(check.get("part", "other")), []).append(check)
    return templates.TemplateResponse(
        request,
        "health_report.html",
        {
            "instance_id": instance_id,
            "report": report,
            "summary": report.get("summary", {}),
            "checks": checks,
            "parts": parts,
        },
    )


@router.get(
    "/partials/runs/{instance_id}/events",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def run_events_partial(
    request: Request,
    instance_id: str,
    after: int = Query(0, ge=0),
) -> HTMLResponse:
    """HTMX polling target — appends new event rows below the previous batch."""
    page = events_svc.read_page(instance_id, after=after)
    return templates.TemplateResponse(request, "_run_events.html", {"page": page})


@router.get(
    "/partials/runs/{instance_id}/timeline",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def run_timeline_partial(request: Request, instance_id: str) -> HTMLResponse:
    """HTMX polling target — refreshed stage / table timeline."""
    timeline = timeline_svc.build_timeline(instance_id)
    return templates.TemplateResponse(request, "_run_timeline.html", {"timeline": timeline})


@router.get(
    "/partials/runs/{instance_id}/pulse",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def run_pulse_partial(request: Request, instance_id: str) -> HTMLResponse:
    """Server-rendered pulse + tiles fragment.

    Used for the initial run-detail render (before SSE connects) and
    as the JS-off fallback. The live JS module then upgrades the DOM
    via targeted textContent writes on stable ids.
    """
    from ovid2.web.services import metrics_svc

    sample = metrics_svc.latest(instance_id) or {}
    return templates.TemplateResponse(
        request, "_live_shell.html", {"sample": sample, "instance_id": instance_id}
    )


@router.get(
    "/partials/runs/{instance_id}/status",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def run_job_status_partial(request: Request, instance_id: str) -> HTMLResponse:
    """HTMX polling target — refreshed job-status badge + failure banner.

    Complements the timeline / events / pulse polls: the job-status
    badge (``job: queued`` / ``running`` / ``failed`` / ``done`` /
    ``cancelled``) and the failure-reason banner (rendered only when
    ``status == 'failed'`` and ``error`` is set) live on the run
    detail page and used to be server-rendered once at page load.
    That meant a browser tab left open through the run's lifecycle
    still displayed ``job: queued`` even after the worker had
    reported a terminal state. This partial re-renders that block
    on each poll; the template embeds a tiny inline script that
    strips the polling attributes off the wrapper once the job
    reaches a terminal state, so the loop halts naturally.
    """
    job = jobs_svc.get_job_by_instance(instance_id)
    queue = jobs_svc.queue_position(job.job_id) if job else None
    return templates.TemplateResponse(
        request, "_run_job_status.html", {"job": job, "queue": queue}
    )


@router.get(
    "/partials/runs/list-status",
    response_class=JSONResponse,
    include_in_schema=False,
)
def runs_list_status_partial(
    ids: str = Query("", max_length=8000),
) -> JSONResponse:
    """Return the current run-list badge for each requested instance.

    The Jobs list is server-rendered once; after ▶ launch only the right
    detail pane is swapped, so a launched draft's row kept its stale
    ``DRAFT`` badge until a full reload. ``live.js`` polls this endpoint
    (comma-separated ``ids``) for rows that are still in a non-terminal
    state and patches their badge text / class in place.
    """
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    statuses = jobs_svc.statuses_for_instances(wanted)
    return JSONResponse(
        {
            iid: runs_svc.list_badge_for_web_status(st)
            for iid, st in statuses.items()
        }
    )



# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


@router.get("/pipeline-packages", response_class=HTMLResponse, include_in_schema=False)
def pipeline_packages_page(request: Request) -> HTMLResponse:
    """List every ``ovid2_pipelines.*`` package (pipeline metadata roster)."""
    return templates.TemplateResponse(
        request, "pipeline_packages.html", {"pipelines": pipelines_meta_svc.list_pipelines()}
    )


@router.get("/pipeline-packages/new", response_class=HTMLResponse, include_in_schema=False)
def pipeline_new_page(request: Request, error: str | None = None) -> HTMLResponse:
    """Step 1 of creating a pipeline — pick a type / template.

    ETL pipelines scaffold a fresh pipeline package from a template;
    the operator then lands on the pipeline editor to wire storage +
    warehouse and personalise the pipeline. The non-ETL kinds
    (analysis / dq_checks / copy / move) don't bind a pipeline
    package, so they link straight to the blank pipeline form.

    Registered ahead of ``/pipeline-packages/{name}`` so the literal
    ``new`` segment is not captured by the ``{name}`` path parameter.
    """
    return templates.TemplateResponse(
        request,
        "pipeline_new.html",
        {
            "templates": templates_svc.list_pipeline_templates(),
            "error": error,
        },
    )


@router.post(
    "/pipeline-packages/new",
    response_class=HTMLResponse,
    response_model=None,
    include_in_schema=False,
)
async def pipeline_new_post(request: Request) -> HTMLResponse | RedirectResponse:
    """Scaffold a pipeline from a template, then open the pipeline editor.

    On error the form is re-rendered in place (HTTP 400) with the
    submitted ``name`` / ``template`` preserved, rather than redirecting
    to a blank form.
    """
    form = {k: str(v) for k, v in (await request.form()).items()}
    name = form.get("name", "").strip()
    template_id = form.get("template", "").strip()
    payload = ScaffoldPipelineRequest(name=name, template=template_id or None)
    try:
        result = templates_svc.scaffold_pipeline(payload, actor="web")
    except (templates_svc.ScaffoldError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "pipeline_new.html",
            {
                "templates": templates_svc.list_pipeline_templates(),
                "error": str(exc),
                "form": form,
            },
            status_code=400,
        )
    # Land on the new pipeline's editor hub so the operator personalises
    # its dictionaries / formatters / fixers before wiring storage.
    return RedirectResponse(
        url=f"/pipeline-packages/{result.name}",
        status_code=303,
    )


@router.get(
    "/pipeline-packages/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_detail_page(request: Request, name: str) -> HTMLResponse:
    """Show metadata + spec roster for one pipeline (its owned package).

    Also surfaces the matching runnable pipeline definition (pipeline
    config) when one exists with the same name, so the hub links its
    trigger + input / output + warehouse alongside the editable
    formatters / mappers / dictionaries / fixers.
    """
    pipeline = name
    detail = pipelines_meta_svc.get_pipeline(pipeline)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline}' not found.")
    pipeline_config = None
    for summary in pipelines_svc.list_pipelines():
        if summary.pipeline == pipeline:
            pipeline_config = summary
            break
    return templates.TemplateResponse(
        request,
        "pipeline_detail.html",
        {"pipeline": detail, "pipeline_config": pipeline_config},
    )


@router.get(
    "/pipeline-packages/{name}/file",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_file_preview_page(
    request: Request,
    name: str,
    relative_path: str = Query(...),
) -> HTMLResponse:
    """Render a whitelisted pipeline-package file as read-only text."""
    pipeline = name
    result = pipelines_meta_svc.read_pipeline_file(pipeline, relative_path)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or not permitted.")
    path, content = result
    return templates.TemplateResponse(
        request,
        "pipeline_file.html",
        {
            "pipeline": pipeline,
            "relative_path": relative_path,
            "absolute_path": str(path),
            "content": content,
            "size_bytes": path.stat().st_size,
        },
    )


# ---------------------------------------------------------------------------
# Formatters / mappers viewer
# ---------------------------------------------------------------------------


@router.get(
    "/pipeline-packages/{name}/formatters/new",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def formatter_new_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new formatter" YAML editor for a pipeline."""
    pipeline = name
    if pipelines_meta_svc.get_pipeline(pipeline) is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline}' not found.")
    return templates.TemplateResponse(
        request,
        "formatter_edit.html",
        {
            "mode": "new",
            "pipeline": pipeline,
            "table": "",
            "raw_text": "adapter: source_passthrough\n",
            "error": error,
        },
    )


@router.get(
    "/pipeline-packages/{name}/formatters/{table}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def formatter_detail_page(
    request: Request,
    name: str,
    table: str,
) -> HTMLResponse:
    """Render one formatter spec (raw text + parsed structure)."""
    pipeline = name
    detail = formatters_svc.get_formatter(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Formatter '{pipeline}/{table}' not found.",
        )
    return templates.TemplateResponse(
        request,
        "formatter_detail.html",
        {"spec": detail},
    )


@router.get(
    "/pipeline-packages/{name}/formatters/{table}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def formatter_edit_page(
    request: Request,
    name: str,
    table: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the YAML editor for one formatter spec."""
    pipeline = name
    detail = formatters_svc.get_formatter(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Formatter '{pipeline}/{table}' not found.",
        )
    if detail.source_kind != "yaml":
        raise HTTPException(
            status_code=400,
            detail=f"Formatter '{pipeline}/{table}' is a Python-dict spec; edit on disk.",
        )
    return templates.TemplateResponse(
        request,
        "formatter_edit.html",
        {
            "mode": "edit",
            "pipeline": pipeline,
            "table": table,
            "raw_text": detail.raw_text,
            "error": error,
        },
    )


@router.post(
    "/pipeline-packages/{name}/formatters/new",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def formatter_create_post(request: Request, name: str) -> RedirectResponse:
    """Save a brand-new formatter YAML spec."""
    pipeline = name
    form = dict(await request.form())
    table = str(form.get("table", "")).strip()
    raw_text = str(form.get("raw_text", ""))
    try:
        payload = FormatterWrite(raw_text=raw_text)
        formatters_svc.save_formatter(pipeline, table, payload, actor="web")
    except (formatters_svc.FormatterError, ValueError) as exc:
        return RedirectResponse(
            url=f"/pipeline-packages/{pipeline}/formatters/new?error={exc}", status_code=303
        )
    return RedirectResponse(
        url=f"/pipeline-packages/{pipeline}/formatters/{table}", status_code=303
    )


@router.post(
    "/pipeline-packages/{name}/formatters/{table}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def formatter_update_post(
    request: Request, name: str, table: str
) -> RedirectResponse:
    """Save an edited formatter YAML spec."""
    pipeline = name
    form = dict(await request.form())
    raw_text = str(form.get("raw_text", ""))
    try:
        payload = FormatterWrite(raw_text=raw_text)
        formatters_svc.save_formatter(pipeline, table, payload, actor="web")
    except (formatters_svc.FormatterError, ValueError) as exc:
        return RedirectResponse(
            url=f"/pipeline-packages/{pipeline}/formatters/{table}/edit?error={exc}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/pipeline-packages/{pipeline}/formatters/{table}", status_code=303
    )


@router.post(
    "/pipeline-packages/{name}/formatters/{table}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def formatter_delete_post(
    request: Request, name: str, table: str
) -> RedirectResponse:
    """Delete a formatter YAML spec."""
    pipeline = name
    try:
        formatters_svc.delete_formatter(pipeline, table, actor="web")
    except formatters_svc.FormatterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/pipeline-packages/{pipeline}", status_code=303)


@router.post(
    "/pipeline-packages/{name}/formatters/validate",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def formatter_validate_partial(request: Request, name: str) -> HTMLResponse:
    """HTMX target on the pipeline detail page — inline validation report."""
    pipeline = name
    report = formatters_svc.validate_pipeline(pipeline)
    return templates.TemplateResponse(
        request,
        "_formatter_validation.html",
        {"report": report, "kind": "formatter"},
    )


@router.get(
    "/pipeline-packages/{name}/mappers/new",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def mapper_new_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new mapper" YAML editor for a pipeline."""
    pipeline = name
    if pipelines_meta_svc.get_pipeline(pipeline) is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline}' not found.")
    return templates.TemplateResponse(
        request,
        "mapper_edit.html",
        {
            "mode": "new",
            "pipeline": pipeline,
            "table": "",
            "raw_text": "ops: []\n",
            "error": error,
        },
    )


@router.get(
    "/pipeline-packages/{name}/mappers/{table}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def mapper_detail_page(
    request: Request,
    name: str,
    table: str,
) -> HTMLResponse:
    """Render one mapper spec."""
    pipeline = name
    detail = mappers_svc.get_mapper(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Mapper '{pipeline}/{table}' not found.",
        )
    return templates.TemplateResponse(
        request,
        "mapper_detail.html",
        {"spec": detail},
    )


@router.get(
    "/pipeline-packages/{name}/mappers/{table}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def mapper_edit_page(
    request: Request,
    name: str,
    table: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the YAML editor for one mapper spec."""
    pipeline = name
    detail = mappers_svc.get_mapper(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Mapper '{pipeline}/{table}' not found.",
        )
    if detail.source_kind != "yaml":
        raise HTTPException(
            status_code=400,
            detail=f"Mapper '{pipeline}/{table}' is a SPEC-dict module; edit on disk.",
        )
    return templates.TemplateResponse(
        request,
        "mapper_edit.html",
        {
            "mode": "edit",
            "pipeline": pipeline,
            "table": table,
            "raw_text": detail.raw_text,
            "error": error,
        },
    )


@router.post(
    "/pipeline-packages/{name}/mappers/new",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def mapper_create_post(request: Request, name: str) -> RedirectResponse:
    """Save a brand-new mapper YAML spec."""
    pipeline = name
    form = dict(await request.form())
    table = str(form.get("table", "")).strip()
    raw_text = str(form.get("raw_text", ""))
    try:
        payload = MapperWrite(raw_text=raw_text)
        mappers_svc.save_mapper(pipeline, table, payload, actor="web")
    except (mappers_svc.MapperError, ValueError) as exc:
        return RedirectResponse(
            url=f"/pipeline-packages/{pipeline}/mappers/new?error={exc}", status_code=303
        )
    return RedirectResponse(
        url=f"/pipeline-packages/{pipeline}/mappers/{table}", status_code=303
    )


@router.post(
    "/pipeline-packages/{name}/mappers/{table}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def mapper_update_post(
    request: Request, name: str, table: str
) -> RedirectResponse:
    """Save an edited mapper YAML spec."""
    pipeline = name
    form = dict(await request.form())
    raw_text = str(form.get("raw_text", ""))
    try:
        payload = MapperWrite(raw_text=raw_text)
        mappers_svc.save_mapper(pipeline, table, payload, actor="web")
    except (mappers_svc.MapperError, ValueError) as exc:
        return RedirectResponse(
            url=f"/pipeline-packages/{pipeline}/mappers/{table}/edit?error={exc}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/pipeline-packages/{pipeline}/mappers/{table}", status_code=303
    )


@router.post(
    "/pipeline-packages/{name}/mappers/{table}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def mapper_delete_post(
    request: Request, name: str, table: str
) -> RedirectResponse:
    """Delete a mapper YAML spec."""
    pipeline = name
    try:
        mappers_svc.delete_mapper(pipeline, table, actor="web")
    except mappers_svc.MapperError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/pipeline-packages/{pipeline}", status_code=303)


@router.post(
    "/pipeline-packages/{name}/mappers/validate",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def mapper_validate_partial(request: Request, name: str) -> HTMLResponse:
    """HTMX target on the pipeline detail page — inline mapper validation."""
    pipeline = name
    report = mappers_svc.validate_pipeline(pipeline)
    return templates.TemplateResponse(
        request,
        "_formatter_validation.html",
        {"report": report, "kind": "mapper"},
    )


# ---------------------------------------------------------------------------
# Jobs — submit / cancel / rerun (W3)
# ---------------------------------------------------------------------------


def _resolve_submit_endpoint(
    mode: str,
    address: str,
    connection: str,
    location: str,
) -> str:
    """Return an address name for one submit endpoint (input or output).

    - ``mode == "browse"``: resolve-or-create an address from the
      chosen storage account (``connection``) + browsed folder
      (``location``). The browsed folder is stored as the address's
      ``location.path``.
    - anything else (``address``): return the saved-address
      ``<select>`` value verbatim.

    Raises:
        addresses_svc.AddressError: When browse mode is chosen but no
            connection was supplied, or the create fails.
    """
    if (mode or "").strip() == "browse":
        conn = (connection or "").strip()
        if not conn:
            raise addresses_svc.AddressError(
                "Pick a storage account to browse before submitting."
            )
        folder = (location or "").strip().strip("/")
        loc: dict[str, object] = {"path": folder} if folder else {}
        return addresses_svc.resolve_or_create_address(conn, loc, actor="web")
    return (address or "").strip()


def _batch_id_from_location(*locations: str) -> str | None:
    """Derive a batch id from the first non-empty browsed location leaf.

    The run modal replaces the free-text batch-id field: the operator
    navigates to a folder (filesystem) or schema (warehouse), and its
    final path segment names the batch. The candidate ``locations`` are
    tried in order (input first, then output); the first that yields a
    non-empty leaf wins. Returns ``None`` when none carry a location, so
    the caller falls back to a synthesised timestamp batch id.
    """
    for raw in locations:
        leaf = (raw or "").strip().strip("/").rsplit("/", 1)[-1].strip()
        if leaf:
            return leaf
    return None


@router.get("/submit", response_class=HTMLResponse, include_in_schema=False)
def submit_form(
    request: Request,
    error: str | None = None,
    flash: str | None = None,
    pipeline: str | None = None,
    batch_id: str | None = None,
    kind: str | None = None,
    engine: str | None = None,
    input_address: str | None = None,
    output_address: str | None = None,
) -> HTMLResponse:
    """Render the "submit run" form.

    The query params pre-select / pre-fill the form — used when an *open*
    pipeline is sent here from its "run now" action so the operator only
    has to choose the open storage side(s). ``pipeline`` is the installed
    package / generic label (as listed in the dropdown), ``input_address``
    / ``output_address`` pre-fill any side that was bound at create time
    (the open side is left blank for the operator to choose).
    """
    from ovid2.cdm.registry import GENERIC_CDM_LABELS
    from ovid2.web.kinds import kind_choices
    from ovid2.web.models import (
        PIPELINE_KINDS,
    )

    pipelines = pipelines_meta_svc.list_pipelines()
    address_names = _addresses.list_available_addresses()
    storage_accounts = connections_svc.list_connections()
    return templates.TemplateResponse(
        request,
        "submit.html",
        {
            "pipelines": pipelines,
            "addresses": address_names,
            "storage_accounts": storage_accounts,
            "pipeline_kinds": PIPELINE_KINDS,
            "kind_choices": kind_choices(),
            "generic_labels": sorted(GENERIC_CDM_LABELS.keys()),
            "health_packages": health_report_svc.list_packages(),
            "error": error,
            "flash": flash,
            "preselect_pipeline": (pipeline or "").strip(),
            "preselect_batch_id": (batch_id or "").strip(),
            "preselect_kind": (kind or "").strip() or None,
            "preselect_engine": (engine or "").strip() or None,
            "preselect_input_address": (input_address or "").strip() or None,
            "preselect_output_address": (output_address or "").strip() or None,
        },
    )


@router.post("/submit", response_class=HTMLResponse, include_in_schema=False)
def submit_form_post(
    request: Request,
    pipeline: str = Form(...),
    batch_id: str = Form(...),
    input_address: str = Form(""),
    output_address: str = Form(""),
    kind: str = Form("etl"),
    tables: str = Form("all"),
    transformations: str = Form("all"),
    packages: str = Form(""),
    checks: str = Form(""),
    engine: str = Form("pyspark"),
    workers: int = Form(25),
    sf_database: str = Form(""),
    sf_account: str = Form(""),
    sf_schema: str = Form(""),
    stderr_log: bool = Form(False),
    extra_mounts: str = Form(""),
    strict_types: bool = Form(False),
    # Storage selection mode. ``address`` (default) uses the saved
    # address <select>; ``browse`` resolves-or-creates an address from
    # the chosen storage account + browsed folder (see
    # ``addresses_svc.resolve_or_create_address``).
    input_mode: str = Form("address"),
    output_mode: str = Form("address"),
    input_connection: str = Form(""),
    input_location: str = Form(""),
    output_connection: str = Form(""),
    output_location: str = Form(""),
) -> RedirectResponse:
    """Handle the submit form; redirect to the run detail page on success."""
    from urllib.parse import quote as _urlquote

    def _error(message: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"/submit?error={_urlquote(str(message))}", status_code=303
        )

    try:
        kind_clean = normalize_kind(kind)

        # Resolve the input / output endpoints. In ``browse`` mode the
        # operator picked a storage account + folder; turn that into a
        # runnable address name (reusing an identical existing address
        # or minting a deterministic new one). In ``address`` mode we
        # use the saved-address <select> verbatim.
        resolved_input = _resolve_submit_endpoint(
            input_mode, input_address, input_connection, input_location
        )
        resolved_output = _resolve_submit_endpoint(
            output_mode, output_address, output_connection, output_location
        )
        if not resolved_input:
            return _error("An input storage account / address is required.")
        if not resolved_output:
            return _error("An output storage account / address is required.")

        # The submit payload shape is registry-driven: ``explicit``-tables
        # kinds (copy / move) accept extra_mounts / strict_types;
        # ``health_selection`` kinds carry a cycle-package picker; every
        # other kind is the ETL shape. See :mod:`ovid2.run_kinds`.
        from ovid2.web.kinds import get_kind as _get_kind

        _submit_spec = _get_kind(kind_clean)
        _explicit_tables = bool(
            _submit_spec and _submit_spec.tables_mode == "explicit"
        )
        _health_shape = bool(_submit_spec and _submit_spec.health_selection)
        if _explicit_tables:
            table_list = [t for t in tables.split() if t and t != "all"]
            if not table_list:
                # ``all`` is accepted only when the input source's driver
                # type can expand it at run time (e.g. Access). Otherwise
                # copy/move need an explicit list.
                from ovid2.web.services._table_expand import (
                    source_supports_expand_all,
                )

                wants_all = "all" in tables.split()
                if wants_all and source_supports_expand_all(resolved_input):
                    # Try to resolve the *concrete* table roster now so the
                    # run page pre-seeds the real table names (like an ETL
                    # run's CDM roster) instead of the opaque ``all``
                    # sentinel. The executor still re-expands ``all`` at
                    # run time, so this is a display/selection nicety: if
                    # enumeration is unavailable in the web tier (no jars /
                    # unreadable file) we fall back to the sentinel and the
                    # run page fills reactively from ``table.*`` events.
                    from ovid2.web.services import source_tables_svc

                    seeded, _supported = source_tables_svc.tables_for_address(
                        resolved_input
                    )
                    table_list = seeded if seeded else ["all"]
                else:
                    return _error(
                        f"{kind_clean} jobs require an explicit tables list "
                        f"('all' is not supported for this source)."
                    )
            # The textarea is one mount per line; strip blanks and
            # comments so operators can annotate.
            mount_list = [
                line.strip()
                for line in (extra_mounts or "").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            payload = JobSubmitRequest(
                kind=kind_clean,
                pipeline=pipeline.strip(),
                batch_id=batch_id.strip(),
                input_address=resolved_input,
                output_address=resolved_output,
                tables=table_list,
                transformations=[],
                engine=engine.strip() or "pyspark",
                workers=int(workers),
                sf_database=None,
                sf_account=None,
                sf_schema=None,
                stderr_log=False,
                extra_mounts=mount_list,
                # strict_types is only honoured on kinds whose spec allows
                # it (move); server-side ``jobs_svc._validate_submit``
                # rejects it elsewhere so forwarding a stray ``True``
                # surfaces a clean 400 rather than a silent change.
                strict_types=(
                    bool(strict_types)
                    if (_submit_spec and _submit_spec.allows_strict_types)
                    else False
                ),
            )
        elif _health_shape:
            # Health reads the CDM tables as-is: no per-table selection,
            # no transformation stages. Selection travels via the
            # cycle-package picker (``packages``) and, optionally, an
            # explicit ``checks`` subset from the Advanced picker (which
            # overrides the package when non-empty); the worker's
            # ``_build_argv_health`` forwards both to ``ovid2 health``.
            package_list = [p for p in packages.split() if p]
            check_list = [c for c in checks.split() if c]
            payload = JobSubmitRequest(
                kind=kind_clean,
                pipeline=pipeline.strip(),
                batch_id=batch_id.strip(),
                input_address=resolved_input,
                output_address=resolved_output,
                tables=[],
                transformations=[],
                packages=package_list,
                checks=check_list,
                engine=engine.strip() or "pyspark",
                workers=int(workers),
                sf_database=sf_database.strip() or None,
                sf_account=sf_account.strip() or None,
                sf_schema=sf_schema.strip() or None,
                stderr_log=bool(stderr_log),
            )
        else:
            table_list = [t for t in tables.split() if t]
            transformation_list = [t for t in transformations.split() if t]
            # Guard against an empty selection. The submit page's
            # checkbox pickers serialise to a space-separated list, or
            # ``all`` when every box is ticked, or the sentinel
            # ``__none__`` when the operator unchecked everything.
            # (FastAPI coerces a bare empty form value back to the
            # ``Form("all")`` default, so JS must send the explicit
            # sentinel rather than "".)
            if tables.strip() == "__none__" or not table_list:
                return _error(
                    "Select at least one table to run."
                )
            if transformations.strip() == "__none__" or not transformation_list:
                return _error(
                    "Select at least one transformation stage to run."
                )
            payload = JobSubmitRequest(
                kind=kind_clean,
                pipeline=pipeline.strip(),
                batch_id=batch_id.strip(),
                input_address=resolved_input,
                output_address=resolved_output,
                tables=table_list,
                transformations=transformation_list,
                engine=engine.strip() or "pyspark",
                workers=int(workers),
                sf_database=sf_database.strip() or None,
                sf_account=sf_account.strip() or None,
                sf_schema=sf_schema.strip() or None,
                stderr_log=bool(stderr_log),
                # extra_mounts is copy/move-only; deliberately not
                # forwarded on the ETL path so the html.py branch
                # mirrors the server-side kind gate in
                # jobs_svc._validate_submit. Same for strict_types.
            )
        response = jobs_svc.submit(payload, actor="web")
    except addresses_svc.AddressError as exc:
        return _error(exc)
    except jobs_svc.SubmitError as exc:
        return _error(exc)
    except (ValueError, TypeError) as exc:  # invalid form encoding
        return _error(exc)
    return RedirectResponse(
        url=f"/runs/{response.instance_id}",
        status_code=303,
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def job_cancel_html(request: Request, job_id: str) -> RedirectResponse:
    """Cancel a job and redirect back to its detail page."""
    try:
        jobs_svc.cancel(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = jobs_svc.get_job(job_id)
    target = f"/runs/{detail.instance_id}" if detail and detail.instance_id else "/runs"
    return RedirectResponse(url=target, status_code=303)


@router.post(
    "/jobs/{job_id}/launch",
    response_class=HTMLResponse,
    response_model=None,
    include_in_schema=False,
)
def job_launch_html(
    request: Request, job_id: str
) -> RedirectResponse | JSONResponse:
    """Launch a draft job (draft → queued) — the run page's ▶ button.

    Mirrors :func:`job_rerun_html`: a background ``X-Requested-With:
    fetch`` POST gets JSON so the console can flip in place; a plain
    form submit gets a 303 back to the same run page.
    """
    try:
        detail = jobs_svc.launch_draft(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.headers.get("x-requested-with", "").lower() == "fetch":
        return JSONResponse(
            {"instance_id": detail.instance_id, "job_id": detail.job_id,
             "status": detail.status},
            status_code=200,
        )
    target = f"/runs/{detail.instance_id}" if detail.instance_id else "/runs"
    return RedirectResponse(url=target, status_code=303)


@router.post(
    "/jobs/{job_id}/discard",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def job_discard_html(request: Request, job_id: str) -> RedirectResponse:
    """Discard a draft job and return to the Jobs list."""
    try:
        jobs_svc.discard_draft(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/runs", status_code=303)


@router.post(
    "/jobs/{job_id}/rerun",
    response_class=HTMLResponse,
    response_model=None,
    include_in_schema=False,
)
def job_rerun_html(
    request: Request, job_id: str
) -> RedirectResponse | JSONResponse:
    """Rerun a job.

    Plain form submits (no-JS fallback) get a 303 redirect to the new
    run's detail page — a full navigation. When the terminal-console
    ▶ Rerun button posts this in the background (``X-Requested-With:
    fetch``), we instead return the new run's ``instance_id`` as JSON so
    the client can swap the new run page in place without a jarring
    full-document reload (see ``static/display/terminal/mount.js`` /
    ``static/live.js``).
    """
    try:
        response = jobs_svc.rerun(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.headers.get("x-requested-with", "").lower() == "fetch":
        return JSONResponse(
            {"instance_id": response.instance_id, "job_id": response.job_id},
            status_code=201,
        )
    return RedirectResponse(
        url=f"/runs/{response.instance_id}",
        status_code=303,
    )


@router.get("/jobs", include_in_schema=False)
def jobs_page(
    request: Request,
    status: str | None = None,
    pipeline: str | None = None,
) -> RedirectResponse:
    """Legacy ``/jobs`` list — merged into the runs workspace.

    The standalone Jobs page was folded into ``/runs`` (one master-detail
    workspace). Preserve the URL as a redirect so bookmarks and any stale
    links keep working; carry the status/pipeline filters through.
    """
    query = []
    if status:
        query.append(f"status={status}")
    if pipeline:
        query.append(f"pipeline={pipeline}")
    target = "/runs" + ("?" + "&".join(query) if query else "")
    return RedirectResponse(url=target, status_code=302)


# ---------------------------------------------------------------------------
# Install wizard (W5)
# ---------------------------------------------------------------------------


@router.get("/install", response_class=HTMLResponse, include_in_schema=False)
def install_page(
    request: Request,
    flash: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Render the install-wizard landing page."""
    status = install_svc.compute_status()
    builds = install_svc.list_image_builds(limit=5, offset=0)
    service_command = install_svc.get_service_command()
    pipelines = pipelines_meta_svc.list_pipelines()
    snowflake_step = next((s for s in status.steps if s.key == "snowflake"), None)
    return templates.TemplateResponse(
        request,
        "install.html",
        {
            "status": status,
            "builds": builds,
            "service_command": service_command,
            "pipelines": pipelines,
            "snowflake_step": snowflake_step,
            "flash": flash,
            "error": error,
        },
    )


@router.post("/install/bootstrap", response_class=HTMLResponse, include_in_schema=False)
def install_bootstrap_html(request: Request) -> RedirectResponse:
    """Run the XDG bootstrap and redirect back to the wizard."""
    try:
        install_svc.run_bootstrap(actor="web")
    except install_svc.InstallError as exc:
        return RedirectResponse(url=f"/install?error={exc}", status_code=303)
    return RedirectResponse(
        url="/install?flash=XDG+tree+ready", status_code=303
    )


@router.post("/install/snowflake", response_class=HTMLResponse, include_in_schema=False)
async def install_snowflake_html(request: Request) -> RedirectResponse:
    """Persist the Snowflake secret entered in the wizard form."""
    form = dict(await request.form())
    values: dict[str, str] = {}
    for key, raw in form.items():
        if not key.startswith("arg_key_"):
            continue
        idx = key.removeprefix("arg_key_")
        val = form.get(f"arg_val_{idx}", "")
        key_name = str(raw).strip()
        if not key_name:
            continue
        values[key_name] = str(val)
    try:
        payload = SecretWrite(values=values)
        install_svc.save_snowflake_secret(payload, actor="web")
    except (install_svc.InstallError, ValueError) as exc:
        return RedirectResponse(url=f"/install?error={exc}", status_code=303)
    return RedirectResponse(
        url="/install?flash=Snowflake+credentials+saved", status_code=303
    )


@router.post(
    "/install/image/build",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def install_image_build_html(request: Request) -> RedirectResponse:
    """Kick off a background docker build from the wizard."""
    form = dict(await request.form())
    tag = str(form.get("tag", "")).strip() or None
    context = str(form.get("context", "")).strip() or None
    dockerfile = str(form.get("dockerfile", "")).strip() or None
    try:
        install_svc.start_image_build(
            tag=tag, context=context, dockerfile=dockerfile, actor="web"
        )
    except install_svc.InstallError as exc:
        return RedirectResponse(url=f"/install?error={exc}", status_code=303)
    return RedirectResponse(
        url="/install?flash=Docker+build+started", status_code=303
    )


@router.get(
    "/install/image/builds/{build_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def install_image_build_page(request: Request, build_id: str) -> HTMLResponse:
    """Detail page for one image build (tail of the log)."""
    detail = install_svc.get_image_build(build_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Image build '{build_id}' not found.")
    log_path = install_svc.image_build_log_path(build_id)
    log_text = ""
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        request,
        "install_image_build.html",
        {"build": detail, "log_text": log_text},
    )


@router.post(
    "/install/pipeline/verify",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def install_pipeline_verify_html(request: Request) -> RedirectResponse:
    """Verify the selected pipeline and flash the outcome."""
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    try:
        install_svc.verify_pipeline(name)
    except install_svc.InstallError as exc:
        return RedirectResponse(url=f"/install?error={exc}", status_code=303)
    return RedirectResponse(
        url=f"/install?flash=Pipeline+{name}+verified", status_code=303
    )


# ---------------------------------------------------------------------------
# Connections (W7)
# ---------------------------------------------------------------------------


def _parse_connection_form(form_body: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split a submitted form into ``(args, secrets)``.

    The connection editor names every argument field ``arg_<key>`` and
    every secret field ``sec_<key>`` so the parser is unambiguous even
    if two fields share a display key by coincidence.
    """
    args: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for k, v in form_body.items():
        value = str(v)
        if k.startswith("arg_"):
            key = k.removeprefix("arg_").strip()
            if key and value != "":
                args[key] = value
        elif k.startswith("sec_"):
            key = k.removeprefix("sec_").strip()
            if key and value != "":
                secrets[key] = value
    return args, secrets


@router.get("/storage", response_class=HTMLResponse, include_in_schema=False)
def storage_page(request: Request) -> HTMLResponse:
    """List every storage account (backed by connection configs)."""
    return templates.TemplateResponse(
        request, "storage.html",
        {"connections": connections_svc.list_connections()},
    )


@router.get("/connections", response_class=HTMLResponse, include_in_schema=False)
def connections_page(request: Request) -> RedirectResponse:
    """Legacy URL — storage accounts are the current concept."""
    return RedirectResponse(url="/storage", status_code=307)


@router.get("/connections/new", response_class=HTMLResponse, include_in_schema=False)
def connection_new_page(
    request: Request,
    type: str = "disc",
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new connection" form with type-appropriate fields."""
    from ovid2.config import connections as _connections
    if type not in _connections.CONNECTION_TYPE_MAP:
        type = "disc"
    spec = _connections.connection_field_spec(type)
    return templates.TemplateResponse(
        request,
        "connection_edit.html",
        {
            "mode": "new",
            "name": "",
            "type": type,
            "all_types": _connections.list_connection_types(),
            "spec": spec,
            "args": {},
            "secrets": {},
            "error": error,
        },
    )


@router.post("/connections", response_class=HTMLResponse, include_in_schema=False)
async def connection_create_post(request: Request) -> RedirectResponse:
    """Handle the "new connection" form submission."""
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    type_name = str(form.get("type", "disc")).strip()
    args, secrets = _parse_connection_form({k: str(v) for k, v in form.items()})
    try:
        payload = ConnectionWrite(type=type_name, args=args, secrets=secrets)
        connections_svc.write_connection(name, payload, actor="web", create_only=True)
    except (connections_svc.ConnectionError, ValueError) as exc:
        return RedirectResponse(
            url=f"/connections/new?type={type_name}&error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/connections/{name}", status_code=303)


@router.get(
    "/connections/{name}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def connection_edit_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the edit form for an existing connection."""
    detail = connections_svc.get_connection(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Connection '{name}' not found.")
    from ovid2.config import connections as _connections
    spec = _connections.connection_field_spec(detail.type)
    return templates.TemplateResponse(
        request,
        "connection_edit.html",
        {
            "mode": "edit",
            "name": name,
            "type": detail.type,
            "all_types": _connections.list_connection_types(),
            "spec": spec,
            "args": detail.args,
            "secrets": detail.secrets,
            "error": error,
        },
    )


@router.post(
    "/connections/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def connection_delete_post(request: Request, name: str) -> RedirectResponse:
    """Delete a connection and return to the index."""
    try:
        connections_svc.delete_connection(name, actor="web")
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/connections", status_code=303)


@router.post(
    "/connections/{name}/test",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def connection_test_post(request: Request, name: str) -> HTMLResponse:
    """Probe a connection and re-render the detail page with the report."""
    detail = connections_svc.get_connection(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Connection '{name}' not found.")
    try:
        report = connections_svc.test_connection(name)
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "connection_detail.html",
        {"connection": detail, "test_result": report},
    )


@router.get(
    "/connections/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def connection_detail_page(request: Request, name: str) -> HTMLResponse:
    """Render one connection detail page (masked secrets)."""
    detail = connections_svc.get_connection(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Connection '{name}' not found.")
    return templates.TemplateResponse(
        request, "connection_detail.html",
        {"connection": detail, "test_result": None},
    )


@router.post(
    "/connections/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def connection_update_post(request: Request, name: str) -> RedirectResponse:
    """Handle the edit-form submission for a connection."""
    form = dict(await request.form())
    type_name = str(form.get("type", "disc")).strip()
    args, secrets = _parse_connection_form({k: str(v) for k, v in form.items()})
    try:
        payload = ConnectionWrite(type=type_name, args=args, secrets=secrets)
        connections_svc.write_connection(name, payload, actor="web")
    except (connections_svc.ConnectionError, ValueError) as exc:
        return RedirectResponse(
            url=f"/connections/{name}/edit?error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/connections/{name}", status_code=303)


# ---------------------------------------------------------------------------
# Engines (E1)
# ---------------------------------------------------------------------------


@router.get("/warehouses", response_class=HTMLResponse, include_in_schema=False)
def warehouses_page(request: Request) -> HTMLResponse:
    """List every warehouse (backed by engine configs)."""
    return templates.TemplateResponse(
        request, "warehouses.html",
        {"engines": engines_svc.list_engines()},
    )


@router.get("/engines", response_class=HTMLResponse, include_in_schema=False)
def engines_page(request: Request) -> RedirectResponse:
    """Legacy URL — warehouses are the current concept."""
    return RedirectResponse(url="/warehouses", status_code=307)


@router.get("/engines/new", response_class=HTMLResponse, include_in_schema=False)
def engine_new_page(
    request: Request,
    type: str = "pyspark",
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new engine" form with type-appropriate fields."""
    from ovid2.config import engines as _engines_config

    known_types = _engines_config.list_engine_types()
    if type not in known_types:
        type = "pyspark"
    spec = _engines_config.engine_field_spec(type)
    return templates.TemplateResponse(
        request,
        "engine_edit.html",
        {
            "mode": "new",
            "name": "",
            "type": type,
            "all_types": known_types,
            "spec": spec,
            "args": {},
            "secrets": {},
            "error": error,
        },
    )


@router.post("/engines", response_class=HTMLResponse, include_in_schema=False)
async def engine_create_post(request: Request) -> RedirectResponse:
    """Handle the "new engine" form submission."""
    form = dict(await request.form())
    name = str(form.get("name", "")).strip()
    type_name = str(form.get("type", "pyspark")).strip()
    args, secrets = _parse_connection_form({k: str(v) for k, v in form.items()})
    try:
        payload = EngineWrite(type=type_name, args=args, secrets=secrets)
        engines_svc.write_engine(name, payload, actor="web", create_only=True)
    except (engines_svc.EngineError, ValueError) as exc:
        return RedirectResponse(
            url=f"/engines/new?type={type_name}&error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/engines/{name}", status_code=303)


@router.get(
    "/engines/{name}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def engine_edit_page(
    request: Request,
    name: str,
    error: str | None = None,
) -> HTMLResponse:
    """Render the edit form for an existing engine."""
    detail = engines_svc.get_engine(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Engine '{name}' not found.")
    from ovid2.config import engines as _engines_config

    spec = _engines_config.engine_field_spec(detail.type)
    return templates.TemplateResponse(
        request,
        "engine_edit.html",
        {
            "mode": "edit",
            "name": name,
            "type": detail.type,
            "all_types": _engines_config.list_engine_types(),
            "spec": spec,
            "args": detail.args,
            "secrets": detail.secrets,
            "error": error,
        },
    )


@router.post(
    "/engines/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def engine_delete_post(request: Request, name: str) -> RedirectResponse:
    """Delete an engine and return to the index."""
    try:
        engines_svc.delete_engine(name, actor="web")
    except engines_svc.EngineError as exc:
        return RedirectResponse(
            url=f"/engines/{name}?error={exc}", status_code=303
        )
    return RedirectResponse(url="/engines", status_code=303)


@router.post(
    "/engines/{name}/test",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def engine_test_post(request: Request, name: str) -> HTMLResponse:
    """Probe an engine and re-render the detail page with the report."""
    detail = engines_svc.get_engine(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Engine '{name}' not found.")
    try:
        report = engines_svc.test_engine(name)
    except engines_svc.EngineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "engine_detail.html",
        {"engine": detail, "test_result": report},
    )


@router.get(
    "/engines/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def engine_detail_page(
    request: Request, name: str, error: str | None = None
) -> HTMLResponse:
    """Render one engine detail page (masked secrets)."""
    detail = engines_svc.get_engine(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Engine '{name}' not found.")
    return templates.TemplateResponse(
        request, "engine_detail.html",
        {"engine": detail, "test_result": None, "error": error},
    )


@router.post(
    "/engines/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def engine_update_post(request: Request, name: str) -> RedirectResponse:
    """Handle the edit-form submission for an engine."""
    form = dict(await request.form())
    type_name = str(form.get("type", "pyspark")).strip()
    args, secrets = _parse_connection_form({k: str(v) for k, v in form.items()})
    try:
        payload = EngineWrite(type=type_name, args=args, secrets=secrets)
        engines_svc.write_engine(name, payload, actor="web")
    except (engines_svc.EngineError, ValueError) as exc:
        return RedirectResponse(
            url=f"/engines/{name}/edit?error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/engines/{name}", status_code=303)


# ---------------------------------------------------------------------------
# CDMs (C1) — read-only inspection pages
# ---------------------------------------------------------------------------


@router.get("/cdms", response_class=HTMLResponse, include_in_schema=False)
def cdms_page(request: Request) -> HTMLResponse:
    """List every CDM registered in :mod:`ovid2.cdm.registry`."""
    return templates.TemplateResponse(
        request, "cdms.html", {"cdms": cdms_svc.list_cdms()}
    )


@router.get(
    "/cdms/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def cdm_detail_page(request: Request, name: str) -> HTMLResponse:
    """Render the detail page for one CDM handle."""
    detail = cdms_svc.get_cdm_detail(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"CDM '{name}' not found.")
    return templates.TemplateResponse(
        request, "cdm_detail.html", {"cdm": detail}
    )


# ---------------------------------------------------------------------------
# Pipelines (W7)
# ---------------------------------------------------------------------------


def _build_pipeline_write(form: dict[str, str]) -> PipelineWrite:
    """Parse a pipeline form dict into a :class:`PipelineWrite`."""
    trigger_type = str(form.get("trigger_type", "manual")).strip() or "manual"
    trigger_kwargs: dict[str, object] = {"type": trigger_type}
    if trigger_type == "cron":
        cron = str(form.get("cron", "")).strip()
        trigger_kwargs["cron"] = cron or None
    if trigger_type == "interval":
        raw = str(form.get("interval_minutes", "")).strip()
        trigger_kwargs["interval_minutes"] = int(raw) if raw else None
    if trigger_type == "watch":
        raw = str(form.get("watch_min_wait_minutes", "")).strip()
        trigger_kwargs["watch_min_wait_minutes"] = int(raw) if raw else None
    cdm_value = str(form.get("cdm", "")).strip() or None
    kind = normalize_kind(form.get("kind"))
    # copy/move cannot use ``all`` (``ovid2 tools copy|move`` has no such
    # sentinel), so never default their tables to ``["all"]`` — an empty
    # field is a valid "open" choice validated as such by
    # ``pipelines_svc._validate_kind``. Every other kind keeps the
    # ``["all"]`` fallback.
    parsed_tables = [t for t in str(form.get("tables", "")).split() if t]
    from ovid2.web.kinds import get_kind as _get_kind

    _spec = _get_kind(kind)
    if _spec and _spec.tables_mode == "explicit":
        _input_addr = str(form.get("input_address", "")).strip()
        if parsed_tables == ["all"]:
            from ovid2.web.services._table_expand import (
                source_supports_expand_all,
            )

            # Preserve the ``all`` sentinel only for sources that can
            # expand it (validated again in ``write_pipeline``); every
            # other explicit source drops a stray ``all`` token.
            tables = (
                ["all"]
                if source_supports_expand_all(_input_addr)
                else []
            )
        else:
            tables = [t for t in parsed_tables if t != "all"]
    else:
        tables = parsed_tables or ["all"]
    return PipelineWrite(
        kind=kind,
        cdm=cdm_value,
        pipeline=str(form.get("pipeline", "")).strip(),
        input_address=str(form.get("input_address", "")).strip(),
        output_address=str(form.get("output_address", "")).strip(),
        tables=tables,
        transformations=[t for t in str(form.get("transformations", "")).split() if t] or ["all"],
        engine=str(form.get("engine", "pyspark")).strip() or "pyspark",
        workers=int(form.get("workers", "25") or 25),
        sf_database=(str(form.get("sf_database", "")).strip() or None),
        sf_account=(str(form.get("sf_account", "")).strip() or None),
        sf_schema=(str(form.get("sf_schema", "")).strip() or None),
        stderr_log=bool(form.get("stderr_log")),
        enabled=bool(form.get("enabled")),
        trigger=PipelineTrigger(**trigger_kwargs),
    )


def _build_new_pipeline_write(
    form: dict[str, str],
    *,
    name: str,
    kind: str,
    cdm: str | None,
    input_address: str,
    output_address: str,
    pipeline: str | None = None,
) -> PipelineWrite:
    """Build a :class:`PipelineWrite` from the simplified new-pipeline form.

    The new-pipeline form asks only for name / type / cdm / storage /
    warehouse / trigger. ``pipeline`` defaults to ``name`` (the package
    is scaffolded under that name for ETL); for ``analysis`` the caller
    passes the chosen existing package so the run reuses its formatters +
    dictionaries.     For ``dq_checks`` the operator picks EDC report-cycle
    packages (the pipeline's 'type') instead — read from the ``packages``
    form field. Tables and transformations default to ``all``; the
    operator refines them later on the edit form.

    ``copy`` / ``move`` are the exception: ``ovid2 tools copy|move``
    cannot expand ``all``, so we never default their tables to ``all``.
    The wizard's copy/move Tables card submits a whitespace-separated
    ``tables`` list (with any stray ``all`` token dropped); an empty
    list is a deliberate "open" choice (tables are picked on the
    ``/submit`` form). ``pipelines_svc._validate_kind`` accepts the
    empty list and rejects only the literal ``['all']``.
    """
    trigger_type = (form.get("trigger_type", "watch").strip() or "watch")
    cron: str | None = None
    interval_minutes: int | None = None
    watch_min_wait_minutes: int | None = None
    if trigger_type == "cron":
        cron = form.get("cron", "").strip() or None
    if trigger_type == "interval":
        raw = form.get("interval_minutes", "").strip()
        interval_minutes = int(raw) if raw else None
    if trigger_type == "watch":
        raw = form.get("watch_min_wait_minutes", "").strip()
        watch_min_wait_minutes = int(raw) if raw else None
    # EDC report-cycle packages arrive as a whitespace/comma-separated
    # list (the radio/checkbox picker submits space-joined ids).
    packages = [
        p.strip()
        for p in form.get("packages", "").replace(",", " ").split()
        if p.strip()
    ]
    # copy/move cannot use ``all`` — read the wizard's explicit list and
    # drop any stray ``all`` token. An empty list is a valid "open"
    # choice (tables picked on the /submit form). Every other kind keeps
    # the ``PipelineWrite`` default of ``["all"]``.
    extra: dict[str, object] = {}
    from ovid2.web.kinds import get_kind as _get_kind

    _spec = _get_kind(kind)
    if _spec and _spec.tables_mode == "explicit":
        extra["tables"] = [
            t for t in form.get("tables", "").split() if t and t != "all"
        ]
    return PipelineWrite(
        kind=kind,
        cdm=cdm,
        pipeline=pipeline or name,
        input_address=input_address,
        output_address=output_address,
        packages=packages,
        engine=form.get("engine", "pyspark").strip() or "pyspark",
        workers=int(form.get("workers", "25") or 25),
        enabled=bool(form.get("enabled")),
        trigger=PipelineTrigger(
            type=trigger_type,
            cron=cron,
            interval_minutes=interval_minutes,
            watch_min_wait_minutes=watch_min_wait_minutes,
        ),
        **extra,
    )


def _list_registered_cdms() -> list[str]:
    """Return the sorted list of registered CDM names for form <select>s.

    Local helper (rather than a module-level import) so
    ``test_web_no_pipeline_imports`` remains satisfied — the CDM
    package is not on its forbidden roots list, but keeping the import
    inline documents the intent that this is a read-only lookup.
    """
    from ovid2.cdm import registered_cdms

    return sorted(registered_cdms())


@router.get("/routes", response_class=HTMLResponse, include_in_schema=False)
def routes_page_redirect() -> RedirectResponse:
    """Back-compat: the legacy ``/routes`` list moved to ``/pipelines``.

    Kept as a 307 redirect so bookmarked ``/routes`` links keep
    resolving after the config object was renamed to *pipeline*.
    """
    return RedirectResponse(url="/pipelines", status_code=307)


@router.get("/pipelines", response_class=HTMLResponse, include_in_schema=False)
def pipelines_page(request: Request) -> HTMLResponse:
    """List every pipeline (backed by pipeline configs), grouped by type."""
    from ovid2.web.kinds import all_kinds, get_kind

    # Kinds whose ``tables`` field is an explicit list with no ``all``
    # sentinel (copy / move). The Run modal shows a tables input only for
    # these so a pipeline created *open* on tables can supply the list
    # ``ovid2 tools copy|move`` requires. Registry-driven — no hardcoded
    # kind names in the template or JS.
    explicit_tables_kinds = sorted(
        name
        for name in all_kinds()
        if (spec := get_kind(name)) and spec.tables_mode == "explicit"
    )
    # Pipeline names whose input source's driver type can expand ``all``
    # (e.g. Access). The Run modal lets these submit an empty (== ``all``)
    # tables field. Registry-driven; no hardcoded type names.
    from ovid2.web.services._table_expand import source_supports_expand_all

    groups = pipelines_svc.list_pipelines_grouped()
    supports_all_names = sorted(
        {
            r.name
            for group in groups
            for r in group.pipelines
            if r.kind in explicit_tables_kinds
            and r.input_address
            and source_supports_expand_all(r.input_address)
        }
    )
    return templates.TemplateResponse(
        request,
        "pipelines.html",
        {
            "groups": groups,
            "pipeline_configs": pipelines_svc.list_pipelines(),
            "storage_accounts": connections_svc.list_connections(),
            "explicit_tables_kinds": explicit_tables_kinds,
            "supports_all_names": supports_all_names,
        },
    )


def _new_pipeline_context(
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the template context for the "new pipeline" form.

    Shared by the GET handler (:func:`pipeline_config_new_page`) and the
    POST error path (:func:`pipeline_config_create_post`) so the choices
    offered (templates, storage accounts, kinds, packages, CDMs) stay in
    sync between a fresh render and a re-render after a validation error.

    ``form`` carries the operator's submitted values back into the
    template so an error never wipes the form — every field re-populates
    from it. It is ``None`` on the initial GET (blank form with static
    defaults) and the raw submitted dict on an error re-render.

    Args:
        error: A flash-error message to surface at the top of the form.
        form: The submitted form values to re-populate fields with, or
            ``None`` for a fresh (blank) form.

    Returns:
        The Jinja2 template context dict.
    """
    from ovid2.web.kinds import kind_choices
    from ovid2.web.models import PIPELINE_KINDS

    return {
        "templates": templates_svc.list_pipeline_templates(),
        "storage_accounts": connections_svc.list_connections(),
        "pipeline_kinds": list(PIPELINE_KINDS),
        "kind_choices": kind_choices(),
        "installed_packages": pipelines_meta_svc.discover_pipelines(),
        "cycle_packages": health_report_svc.list_packages(),
        "cdms": _list_registered_cdms(),
        # Configured engines power the new-pipeline builder's engine
        # <select> and the canvas "material" (each summary carries its
        # ``type`` — pyspark / databricks_spark / snowpark — which the
        # canvas maps to a body texture). Read-only summaries only; no
        # pipeline/core import (keeps test_web_no_pipeline_imports green).
        "engines": engines_svc.list_engines(),
        # Every *registered* engine type (pyspark / databricks_spark /
        # snowpark), independent of whether a config instance exists yet.
        # The builder lists these so the operator can always pick any
        # engine kind — a fresh install with no saved engine configs is
        # not limited to the built-in pyspark default.
        "engine_types": [t.type for t in engines_svc.list_engine_types()],
        "error": error,
        "form": form,
    }


@router.get("/pipelines/new", response_class=HTMLResponse, include_in_schema=False)
def pipeline_config_new_page(
    request: Request,
    error: str | None = None,
) -> HTMLResponse:
    """Render the "new pipeline" form.

    The create form asks only for what a pipeline needs: a name, a
    pipeline type (kind), a CDM + template (ETL only), input / output
    storage accounts (browsed with the storage picker), a warehouse, and
    a trigger (defaulting to ``watch``). For ETL the matching pipeline
    package is scaffolded from the chosen template on submit — the
    operator never picks from a list of installed packages (the package
    is named after the pipeline). See :func:`pipeline_config_create_post`.
    """
    return templates.TemplateResponse(
        request,
        "pipeline_new_config.html",
        _new_pipeline_context(error=error),
    )


@router.post(
    "/pipelines",
    response_class=HTMLResponse,
    response_model=None,
    include_in_schema=False,
)
async def pipeline_config_create_post(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Create a brand-new pipeline.

    For ``kind=etl`` this first scaffolds a pipeline package named after
    the pipeline (cloning the chosen template) so the config's
    ``pipeline`` field — which the worker uses to select the
    formatter/mapper/fixer package — resolves to a real, installed
    package. Every kind binds ``pipeline == name``; there is no
    package-selection dropdown.

    On any validation error the form is **re-rendered in place** (HTTP
    400) with the operator's submitted values threaded back through the
    template, so a mistake never wipes what they entered. Only a
    successful create redirects (Post/Redirect/Get) to the new pipeline.
    """
    raw_form = await request.form()
    form = {k: str(v) for k, v in raw_form.items()}
    # ``packages`` is a multi-value checkbox group (the EDC cycle picker
    # for dq_checks). ``.items()`` above collapses repeats to the last
    # value, so re-read all of them and fold into a single space-joined
    # string — both the config builder and the error re-render then read
    # ``form['packages']`` uniformly.
    picked_packages = [str(v) for v in raw_form.getlist("packages") if str(v).strip()]
    if picked_packages:
        form["packages"] = " ".join(picked_packages)
    name = form.get("name", "").strip()

    def _error(message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "pipeline_new_config.html",
            _new_pipeline_context(error=message, form=form),
            status_code=400,
        )

    kind = normalize_kind(form.get("kind"))
    cdm = form.get("cdm", "").strip() or None
    # The create flow is registry-driven: each kind's
    # :attr:`~ovid2.run_kinds.KindSpec.create_flow` selects one of four
    # behaviours. ``name`` is always the config label; ``pipeline`` is
    # the installed package (``pick_package``) or the label itself
    # (every other flow).
    from ovid2.web.kinds import get_kind as _get_kind

    _spec = _get_kind(kind)
    _create_flow = _spec.create_flow if _spec else "plain"
    pipeline_name = name
    try:
        # scaffold_package (etl): scaffold a package = the pipeline name
        # from a template — unless a package with that name already
        # exists (e.g. scaffolded earlier via /pipeline-packages/new or
        # the CLI), in which case reuse it and only create the config.
        if _create_flow == "scaffold_package" and name not in set(
            pipelines_meta_svc.discover_pipelines()
        ):
            template_id = form.get("template", "").strip()
            if not template_id:
                return _error("Pick an ETL template.")
            scaffold = templates_svc.scaffold_pipeline(
                ScaffoldPipelineRequest(name=name, template=template_id),
                actor="web",
            )
            # Default the CDM to the template's target when unset.
            cdm = cdm or scaffold.cdm
        elif _create_flow == "pick_package":
            # analysis runs over an existing installed package, chosen on
            # the form. ``pipeline`` is that package; ``name`` remains the
            # config label so several configs can target one package.
            pipeline_name = form.get("package", "").strip()
            if not pipeline_name:
                return _error(
                    "Pick an existing pipeline package to run analysis against."
                )
        elif _create_flow == "cycle_packages":
            # dq_checks is package-free: it runs EDC report-cycle checks
            # (the pipeline's 'type'), so require at least one cycle
            # package selection. ``pipeline`` stays bound to ``name``.
            picked = [
                p
                for p in form.get("packages", "").replace(",", " ").split()
                if p.strip()
            ]
            if not picked:
                return _error("Pick at least one EDC report cycle to run.")

        # 2. Resolve input / output storage accounts into address names.
        #    A side flagged "open" (input_open / output_open == "1") is
        #    left unbound (empty address) — the operator supplies it at
        #    run time on the /submit form, which requires it there.
        input_open = form.get("input_open", "0").strip() in ("1", "true", "on")
        output_open = form.get("output_open", "0").strip() in ("1", "true", "on")
        if not input_open and not form.get("input_connection", "").strip():
            return _error("Pick an input storage account, or keep input open.")
        if not output_open and not form.get("output_connection", "").strip():
            return _error("Pick an output storage account, or keep output open.")
        resolved_input = "" if input_open else _resolve_submit_endpoint(
            "browse",
            form.get("input_address", ""),
            form.get("input_connection", ""),
            form.get("input_location", ""),
        )
        resolved_output = "" if output_open else _resolve_submit_endpoint(
            "browse",
            form.get("output_address", ""),
            form.get("output_connection", ""),
            form.get("output_location", ""),
        )

        # 3. Build the config. ``pipeline`` is the chosen package for
        #    analysis/dq_checks, else it is bound to ``name``.
        payload = _build_new_pipeline_write(
            form,
            name=name,
            kind=kind,
            cdm=cdm,
            input_address=resolved_input,
            output_address=resolved_output,
            pipeline=pipeline_name,
        )
        pipelines_svc.write_pipeline(name, payload, actor="web", create_only=True)
    except (
        pipelines_svc.PipelineError,
        templates_svc.ScaffoldError,
        addresses_svc.AddressError,
        ValueError,
    ) as exc:
        return _error(str(exc))
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)


@router.get(
    "/pipelines/{name}/edit",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_config_edit_page(
    request: Request, name: str, error: str | None = None
) -> HTMLResponse:
    """Render the edit form for an existing pipeline."""
    detail = pipelines_svc.get_pipeline(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found.")
    from ovid2.config import addresses as _addresses
    from ovid2.web.kinds import kind_choices
    from ovid2.web.models import PIPELINE_KINDS
    return templates.TemplateResponse(
        request,
        "pipeline_edit.html",
        {
            "mode": "edit",
            "name": name,
            "pipeline_config": detail,
            "addresses": _addresses.list_available_addresses(),
            "pipeline_kinds": list(PIPELINE_KINDS),
            "kind_choices": kind_choices(),
            "cdms": _list_registered_cdms(),
            "error": error,
        },
    )


@router.post(
    "/pipelines/{name}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_config_delete_post(request: Request, name: str) -> RedirectResponse:
    """Delete a pipeline and return to the index."""
    try:
        pipelines_svc.delete_pipeline(name, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/pipelines", status_code=303)


@router.post(
    "/pipelines/{name}/run",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_config_run_post(
    request: Request,
    name: str,
    batch_id: str = Form(""),
    input_connection: str = Form(""),
    output_connection: str = Form(""),
    input_location: str = Form(""),
    output_location: str = Form(""),
    tables: str = Form(""),
) -> RedirectResponse:
    """Submit the pipeline as a job and redirect to the run detail page.

    ``batch_id`` is normally *derived* from the browsed folder / schema
    (the run modal's navigator no longer lets the operator type one): the
    final path segment of the chosen input location — or, failing that,
    the output location — names the batch. When neither open side yields
    a location leaf a timestamp batch id is synthesised (the plain "run
    now" behaviour). The batch id doubles as the schema to read from /
    write to. A ``batch_id`` posted explicitly (e.g. by the JSON API or a
    legacy caller) still wins.

    A pipeline that keeps its input or output *open* (unbound at create
    time) has no storage to run against. The operator binds the open
    side(s) in the run modal by browsing to a storage account
    (``input_connection`` / ``output_connection``) and folder / schema
    (``input_location`` / ``output_location``), which is resolved-or-
    created into an address for this one run. When an open side is left
    unbound here (e.g. the bare "run now" button, or the pipeline-detail
    link), the run is redirected to the Submit form (pre-selected on this
    pipeline) so the operator can choose it there.

    ``tables`` is a one-run override for ``copy`` / ``move``
    (``explicit``-tables) pipelines created *open* on tables: the run
    modal shows a whitespace-separated tables field (logical or
    ``LOGICAL=SOURCE`` entries) whose value is forwarded to ``run_now``.
    Empty / absent keeps the config's stored tables, so etl-shape kinds
    are unaffected. A stray ``all`` token is dropped (``ovid2 tools
    copy|move`` has no ``all`` sentinel).
    """
    from urllib.parse import quote as _urlquote

    detail = pipelines_svc.get_pipeline(name)
    if detail is not None:
        input_open = not (detail.input_address or "").strip()
        output_open = not (detail.output_address or "").strip()
        input_conn = (input_connection or "").strip()
        output_conn = (output_connection or "").strip()
        input_loc = (input_location or "").strip()
        output_loc = (output_location or "").strip()

        # ``tables`` is the run modal's one-run override for copy / move
        # (``explicit``-tables) pipelines created *open* on tables: the
        # field carries a whitespace-separated list of logical /
        # ``LOGICAL=SOURCE`` entries. Parse it into a list, dropping any
        # stray ``all`` token (``ovid2 tools copy|move`` has no ``all``
        # sentinel). ``None`` (the field absent / empty) keeps the
        # config's stored tables so etl-shape kinds are unaffected.
        parsed_tables = [t for t in (tables or "").split() if t and t != "all"]
        _wants_all = "all" in (tables or "").split()
        tables_override: list[str] | None = (
            parsed_tables if (tables or "").strip() else None
        )

        # Inline binding: an open side with a storage account chosen in
        # the run modal is resolved-or-created into an address (storing
        # the browsed folder / schema as the address location) and passed
        # to run_now as a one-run override.
        override_input: str | None = None
        override_output: str | None = None
        try:
            if input_open and input_conn:
                override_input = _resolve_submit_endpoint(
                    "browse", "", input_conn, input_loc
                )
            if output_open and output_conn:
                override_output = _resolve_submit_endpoint(
                    "browse", "", output_conn, output_loc
                )
        except addresses_svc.AddressError as exc:
            return RedirectResponse(
                url=f"/pipelines?error={_urlquote(str(exc))}", status_code=303
            )

        # A side that is still open (no inline pick) falls back to the
        # Submit form, where the operator can browse to a sub-folder.
        still_open_input = input_open and override_input is None
        still_open_output = output_open and override_output is None
        if still_open_input or still_open_output:
            # The Submit form's pipeline dropdown lists installed
            # packages / generic labels — i.e. ``detail.pipeline`` — not
            # config names, so preselect the underlying package/label.
            # Carry kind + engine + any bound (or inline-picked) side +
            # the derived batch id along.
            params = [f"pipeline={_urlquote(detail.pipeline)}"]
            if detail.kind:
                params.append(f"kind={_urlquote(detail.kind)}")
            if detail.engine:
                params.append(f"engine={_urlquote(detail.engine)}")
            bound_input = override_input or (detail.input_address or "").strip()
            bound_output = override_output or (detail.output_address or "").strip()
            if bound_input:
                params.append(f"input_address={_urlquote(bound_input)}")
            if bound_output:
                params.append(f"output_address={_urlquote(bound_output)}")
            if (batch_id or "").strip():
                params.append(f"batch_id={_urlquote(batch_id.strip())}")
            if parsed_tables:
                params.append(f"tables={_urlquote(' '.join(parsed_tables))}")
            return RedirectResponse(
                url="/submit?" + "&".join(params), status_code=303
            )

        # Derive the batch id from the browsed location leaf when the
        # operator did not type one (the run modal does not offer a free
        # text field). Input side wins over output. When neither open
        # side carried a location, ``chosen_batch`` stays None and
        # run_now synthesises a timestamp.
        chosen_batch = (batch_id or "").strip() or _batch_id_from_location(
            input_loc if input_open else "",
            output_loc if output_open else "",
        )
        # Preserve an ``all`` sentinel for this run when the (overridden
        # or config) source can expand it (e.g. Access). Otherwise the
        # stray ``all`` was already dropped from ``tables_override``.
        if _wants_all and not tables_override:
            from ovid2.web.services._table_expand import (
                source_supports_expand_all,
            )

            _run_source = override_input or (detail.input_address or "").strip()
            if source_supports_expand_all(_run_source):
                tables_override = ["all"]
        try:
            response = pipelines_svc.run_now(
                name,
                actor="web",
                batch_id=chosen_batch,
                input_address=override_input,
                output_address=override_output,
                tables=tables_override,
            )
        except pipelines_svc.PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            url=f"/runs/{response.instance_id}",
            status_code=303,
        )

    chosen_batch = (batch_id or "").strip() or None
    tables_override = [t for t in (tables or "").split() if t and t != "all"] or None
    if tables_override is None and "all" in (tables or "").split():
        # A fully-bound copy/move pipeline whose source can expand ``all``
        # (e.g. Access) keeps the sentinel for this run.
        from ovid2.web.services._table_expand import source_supports_expand_all

        _bound = pipelines_svc.get_pipeline(name)
        if _bound and source_supports_expand_all(_bound.input_address or ""):
            tables_override = ["all"]
    try:
        response = pipelines_svc.run_now(
            name, actor="web", batch_id=chosen_batch, tables=tables_override
        )
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/runs/{response.instance_id}",
        status_code=303,
    )


@router.post(
    "/pipelines/{name}/enable",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_config_enable_post(request: Request, name: str) -> RedirectResponse:
    """Enable a pipeline and redirect to its detail page."""
    try:
        pipelines_svc.set_enabled(name, True, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)


@router.post(
    "/pipelines/{name}/disable",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def pipeline_config_disable_post(request: Request, name: str) -> RedirectResponse:
    """Disable a pipeline and redirect to its detail page."""
    try:
        pipelines_svc.set_enabled(name, False, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)


@router.get("/pipelines/{name}", response_class=HTMLResponse, include_in_schema=False)
def pipeline_config_detail_page(request: Request, name: str, flash: str | None = None) -> HTMLResponse:
    """Render one pipeline's detail page."""
    detail = pipelines_svc.get_pipeline(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found.")
    return templates.TemplateResponse(
        request, "pipeline_config_detail.html",
        {"pipeline_config": detail, "flash": flash},
    )


@router.post(
    "/pipelines/{name}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def pipeline_config_update_post(request: Request, name: str) -> RedirectResponse:
    """Save an edited pipeline."""
    form = dict(await request.form())
    try:
        payload = _build_pipeline_write({k: str(v) for k, v in form.items()})
        pipelines_svc.write_pipeline(name, payload, actor="web")
    except (pipelines_svc.PipelineError, ValueError) as exc:
        return RedirectResponse(
            url=f"/pipelines/{name}/edit?error={exc}", status_code=303
        )
    return RedirectResponse(url=f"/pipelines/{name}", status_code=303)


# ---------------------------------------------------------------------------
# Settings (W7)
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(
    request: Request,
    flash: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Render the global settings form."""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings_svc.get_settings(),
            "connections": connections_svc.list_connections(),
            "flash": flash,
            "error": error,
        },
    )


@router.post("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_post(
    request: Request, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Persist the settings form."""
    form = dict(await request.form())
    # Preserve subsections that the form does not surface (ods,
    # fs_picker) so a save through this page doesn't silently reset
    # them to defaults.
    current = settings_svc.get_settings()
    try:
        payload = GlobalSettings(
            defaults=SettingsDefaults(
                engine=str(form.get("defaults_engine", "pyspark")).strip() or "pyspark",
                workers=int(form.get("defaults_workers", "25") or 25),
            ),
            worker=SettingsWorker(
                poll_interval_seconds=float(form.get("worker_poll", "2.0") or 2.0),
                route_dispatch_enabled=bool(form.get("route_dispatch_enabled")),
            ),
            ui=SettingsUi(
                dashboard_poll_ms=int(form.get("ui_dash", "3000") or 3000),
                run_events_poll_ms=int(form.get("ui_run", "2000") or 2000),
                display_timezone=(
                    str(form.get("ui_tz", "America/New_York")).strip()
                    or "America/New_York"
                ),
            ),
            notifications=SettingsNotifications(
                smtp_host=str(form.get("smtp_host", "")).strip(),
                smtp_port=int(form.get("smtp_port", "25") or 25),
                smtp_from=str(form.get("smtp_from", "")).strip(),
                smtp_to=[t for t in str(form.get("smtp_to", "")).split() if t],
                smtp_secret_ref=(str(form.get("smtp_secret_ref", "")).strip() or None),
            ),
            retention=SettingsRetention(
                event_log_days=int(form.get("event_log_days", "30") or 30),
                web_jobs_days=int(form.get("web_jobs_days", "90") or 90),
            ),
            ods=current.ods,
            fs_picker=current.fs_picker,
            llm=current.llm,
            admin=SettingsAdmin(
                storage_account=(
                    str(form.get("admin_storage_account", "")).strip() or None
                ),
            ),
        )
        settings_svc.save_settings(payload, actor="web")
    except (settings_svc.SettingsError, ValueError) as exc:
        return RedirectResponse(url=f"/settings?error={exc}", status_code=303)
    return RedirectResponse(url="/settings?flash=Saved", status_code=303)


# ---------------------------------------------------------------------------
# Ovid Data Space
# ---------------------------------------------------------------------------


@router.get("/dataspace", response_class=HTMLResponse, include_in_schema=False)
def dataspace_root_page(request: Request) -> HTMLResponse:
    """Render the root of the Ovid Data Space (pipeline list)."""
    listing = dataspace_svc.list_pipelines()
    catalog = dataspace_svc.get_dimension_catalog()
    return templates.TemplateResponse(
        request,
        "dataspace_root.html",
        {"listing": listing, "catalog": catalog},
    )


@router.get(
    "/dataspace/{pipeline}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def dataspace_pipeline_page(request: Request, pipeline: str) -> HTMLResponse:
    """Render the batches under ``/{pipeline}``."""
    try:
        listing = dataspace_svc.list_pipeline_batches(pipeline)
    except dataspace_svc.OdsServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "dataspace_pipeline.html",
        {"listing": listing},
    )


@router.get(
    "/dataspace/{pipeline}/{batch_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def dataspace_batch_page(
    request: Request, pipeline: str, batch_id: str
) -> HTMLResponse:
    """Render the tables under ``/{pipeline}/{batch_id}``."""
    try:
        listing = dataspace_svc.list_batch_tables(pipeline, batch_id)
    except dataspace_svc.OdsServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "dataspace_batch.html",
        {"listing": listing},
    )


# ---------------------------------------------------------------------------
# Filesystem picker HTMX partial
# ---------------------------------------------------------------------------


@router.get("/fs/picker", response_class=HTMLResponse, include_in_schema=False)
def fs_picker_partial(
    request: Request,
    target: str = Query(..., description="ID of the form field to populate."),
    path: str | None = Query(None, description="Directory to list."),
    show_hidden: bool | None = Query(None),
) -> HTMLResponse:
    """Render the directory-picker modal body for an HTMX swap."""
    context: dict[str, object] = {"target": target}
    try:
        roots = fs_svc.list_roots()
    except Exception:
        roots = None
    context["roots"] = roots.roots if roots is not None else []
    try:
        listing = fs_svc.list_dir(path, show_hidden=show_hidden)
    except fs_svc.FsPickerDisabledError as exc:
        context["error"] = f"Filesystem picker is disabled: {exc}"
        return templates.TemplateResponse(request, "_fs_picker.html", context)
    except fs_svc.FsNotFoundError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "_fs_picker.html", context)
    except fs_svc.FsAccessError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "_fs_picker.html", context)
    context["listing"] = listing
    return templates.TemplateResponse(request, "_fs_picker.html", context)
