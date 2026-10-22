"""JSON REST API — versioned under ``/api/v1``.

Every mutation and query the UI performs has a matching REST endpoint;
the MCP server later wraps these endpoints as tools. The single source
of truth for request/response shapes is :mod:`ovid2.web.models`.

W1 exposes read-only endpoints. Write endpoints (submit, cancel,
formatter update, address CRUD, ...) land in later phases.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from ovid2.web.auth.deps import AuthContext, require_capability
from ovid2.web.auth.roles import Capability, Role
from ovid2.web.models import (
    AddressDetail,
    AddressSummary,
    AddressWrite,
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalSummary,
    BootstrapRequest,
    BootstrapResponse,
    CdmDetail,
    CdmSummary,
    ConnectionDetail,
    ConnectionSummary,
    ConnectionTestReport,
    ConnectionTypeSpec,
    ConnectionWrite,
    EngineDetail,
    EngineSummary,
    EngineTestReport,
    EngineTypeSpec,
    EngineWrite,
    FormatterSpecDetail,
    FormatterValidationReport,
    FormatterWrite,
    FsListing,
    FsRoots,
    GlobalSettings,
    HealthResponse,
    ImageBuildDetail,
    ImageBuildListResponse,
    ImageBuildRequest,
    InstallServiceCommand,
    InstallStatus,
    JobDetail,
    JobListResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    LlmChatRequest,
    LlmDetail,
    LlmProviderSpec,
    LlmStatus,
    LlmSummary,
    LlmTestReport,
    LlmToolInfo,
    LlmWrite,
    MapperSpecDetail,
    MapperValidationReport,
    MapperWrite,
    OdsBatchListing,
    OdsDimensionCatalog,
    OdsPipelineListing,
    OdsResolveResult,
    OdsTableListing,
    PipelineConfigDetail,
    PipelineConfigSummary,
    PipelineDetail,
    PipelineFilePreview,
    PipelinePlanResponse,
    PipelineRunRequest,
    PipelineSummary,
    PipelineTemplateSummary,
    PipelineVerifyResponse,
    PipelineWrite,
    RunEventPage,
    RunListResponse,
    RunSummary,
    RunTimeline,
    ScaffoldPipelineRequest,
    ScaffoldPipelineResponse,
    SecretDetail,
    SecretSummary,
    SecretWrite,
    SnowflakeSaveResponse,
    SourceTablesResponse,
    StorageBrowseResponse,
    TransformSummary,
    TransformValidateReport,
    TransformValidateRequest,
    UserActivateRequest,
    UserDetail,
    UserRoleRequest,
    UserSummary,
    WriteResult,
)
from ovid2.web.services import (
    addresses_svc,
    approvals_svc,
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
    llm_chat_svc,
    llms_svc,
    mappers_svc,
    pipeline_plan_svc,
    pipelines_meta_svc,
    pipelines_svc,
    runs_svc,
    secrets_svc,
    settings_svc,
    source_tables_svc,
    storage_browse_svc,
    templates_svc,
    timeline_svc,
    transforms_svc,
    users_svc,
)
from ovid2.web.settings import LLM_TOOLS_API_URL, RUN_EVENTS_PAGE_SIZE

router = APIRouter(prefix="/api/v1", tags=["v1"])


def _gate(action: str, target: str | None, requested_by: str = "web") -> None:
    """Enforce the destructive-op approval gate, mapping to HTTP 202.

    Calls :func:`approvals_svc.require_approval`; when no approval exists
    yet, translates :class:`ApprovalRequired` into a 202 response whose
    body names the ``approval_id`` a human must approve. See
    the adr-harness-api-executor-trust-model design note.
    """
    try:
        approvals_svc.require_approval(action, target, requested_by=requested_by)
    except approvals_svc.ApprovalRequired as exc:
        raise HTTPException(
            status_code=202,
            detail={
                "status": "awaiting_approval",
                "approval_id": exc.approval_id,
                "action": exc.action,
                "target": exc.target,
            },
        ) from exc


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Environment diagnostics",
    dependencies=[Depends(require_capability(Capability.VIEW_PUBLIC))],
)
def api_health() -> HealthResponse:
    """Return the aggregate doctor report for the current host."""
    return health_svc.collect_health()


@router.get(
    "/addresses",
    response_model=list[AddressSummary],
    summary="List address configs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_addresses() -> list[AddressSummary]:
    """Return every JSON address config discoverable via the XDG search path."""
    return addresses_svc.list_addresses()


@router.get(
    "/addresses/{name}",
    response_model=AddressDetail,
    summary="Get one address config",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_address(name: str) -> AddressDetail:
    """Return the parsed contents of one address config (secrets masked)."""
    detail = addresses_svc.get_address(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Address config '{name}' not found.")
    return detail


@router.post(
    "/addresses/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create an address config",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_create_address(name: str, payload: AddressWrite) -> WriteResult:
    """Create a new address JSON config in the user-scoped configs dir."""
    try:
        return addresses_svc.write_address(name, payload, actor="web", create_only=True)
    except addresses_svc.AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/addresses/{name}",
    response_model=WriteResult,
    summary="Create or update an address config",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_address(name: str, payload: AddressWrite) -> WriteResult:
    """Idempotent create-or-update of an address JSON config."""
    try:
        return addresses_svc.write_address(name, payload, actor="web")
    except addresses_svc.AddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/addresses/{name}",
    response_model=WriteResult,
    summary="Delete an address config",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_address(name: str) -> WriteResult:
    """Remove an address JSON config from the user-scoped configs dir."""
    _gate("address.delete", name)
    try:
        return addresses_svc.delete_address(name, actor="web")
    except addresses_svc.AddressError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Secrets (W4)
# ---------------------------------------------------------------------------


@router.get(
    "/secrets",
    response_model=list[SecretSummary],
    summary="List secret files (metadata only — no values)",
    dependencies=[Depends(require_capability(Capability.MANAGE_SECRETS))],
)
def api_list_secrets() -> list[SecretSummary]:
    """Return every secret file under the user-scoped ``secrets.d/``."""
    return secrets_svc.list_secrets()


@router.get(
    "/secrets/{name}",
    response_model=SecretDetail,
    summary="Get secret shape (keys only)",
    dependencies=[Depends(require_capability(Capability.MANAGE_SECRETS))],
)
def api_get_secret(name: str) -> SecretDetail:
    """Return the top-level key names of a secret; values are never returned."""
    detail = secrets_svc.get_secret(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found.")
    return detail


@router.post(
    "/secrets/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create a secret (0o600 enforced)",
    dependencies=[Depends(require_capability(Capability.MANAGE_SECRETS))],
)
def api_create_secret(name: str, payload: SecretWrite) -> WriteResult:
    """Write a new secret JSON file with mode 0o600."""
    try:
        return secrets_svc.write_secret(name, payload, actor="web", create_only=True)
    except secrets_svc.SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/secrets/{name}",
    response_model=WriteResult,
    summary="Create or update a secret (0o600 enforced)",
    dependencies=[Depends(require_capability(Capability.MANAGE_SECRETS))],
)
def api_put_secret(name: str, payload: SecretWrite) -> WriteResult:
    """Idempotent create-or-update of a secret JSON file (mode 0o600)."""
    try:
        return secrets_svc.write_secret(name, payload, actor="web")
    except secrets_svc.SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/secrets/{name}",
    response_model=WriteResult,
    summary="Delete a secret",
    dependencies=[Depends(require_capability(Capability.MANAGE_SECRETS))],
)
def api_delete_secret(name: str) -> WriteResult:
    """Remove a secret from the user-scoped ``secrets.d/`` directory.

    Gated: destructive, so it requires a prior human approval
    (harness / API / executor trust model). Returns HTTP 202 with the
    ``approval_id`` when no approval exists yet.
    """
    _gate("secret.delete", name)
    try:
        return secrets_svc.delete_secret(name, actor="web")
    except secrets_svc.SecretError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/pipeline-packages",
    response_model=list[PipelineSummary],
    summary="List pipeline packages",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_pipelines() -> list[PipelineSummary]:
    """Return every discoverable ``ovid2_pipelines.*`` package."""
    return pipelines_meta_svc.list_pipelines()


@router.get(
    "/pipeline-templates",
    response_model=list[PipelineTemplateSummary],
    summary="List pipeline templates",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_pipeline_templates() -> list[PipelineTemplateSummary]:
    """Return the curated pipeline templates (Phase 3 scaffolder).

    Each template names a source pipeline package + target CDM that a
    new ETL pipeline can be scaffolded from via ``POST
    /api/v1/pipelines/scaffold``.
    """
    return templates_svc.list_pipeline_templates()


@router.post(
    "/pipeline-packages/scaffold",
    response_model=ScaffoldPipelineResponse,
    status_code=201,
    summary="Scaffold a new pipeline package from a template",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_scaffold_pipeline(
    request: ScaffoldPipelineRequest,
) -> ScaffoldPipelineResponse:
    """Copy a template (or explicit source pipeline) under a new name.

    The new package inherits the template's formatters, dictionaries,
    and fixer chains; its ``pipeline_settings`` identity fields (DB
    name, HARVEST datamart id/name, DATAVANT key) are reset to
    name-derived placeholders and the target CDM binding is preserved.
    """
    try:
        return templates_svc.scaffold_pipeline(request, actor="api")
    except templates_svc.ScaffoldError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(
    "/cdms",
    response_model=list[CdmSummary],
    summary="List registered CDMs",
    dependencies=[Depends(require_capability(Capability.VIEW_PUBLIC))],
)
def api_list_cdms() -> list[CdmSummary]:
    """Return every CDM registered in ``ovid2.cdm.registry`` (phase C1).

    Response items shape:

    ``{name, version, table_count, has_yaml}``

    Used by the routes UI to populate the ``cdm`` <select>, by the
    dedicated CDMs page, and by MCP clients that need to enumerate the
    CDMs Ovid2 knows about.
    """
    return cdms_svc.list_cdms()


@router.get(
    "/cdms/{name}",
    response_model=CdmDetail,
    summary="Get CDM detail",
    dependencies=[Depends(require_capability(Capability.VIEW_PUBLIC))],
)
def api_get_cdm(name: str) -> CdmDetail:
    """Return full metadata for one CDM handle.

    Includes the ordered table list with composite keys, id columns,
    reference tables, flavour-of-null sentinels, shared views, and a
    catalogue of registered valuesets. Values in ``flavour_of_null``
    are the raw sentinels — a JSON ``null`` here means the CDM treats
    database-NULL as one of the flavour-of-null values.
    """
    detail = cdms_svc.get_cdm_detail(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"CDM '{name}' not found.")
    return detail


@router.get(
    "/pipeline-packages/{pipeline}",
    response_model=PipelineDetail,
    summary="Get pipeline detail",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_pipeline(pipeline: str) -> PipelineDetail:
    """Return metadata + spec roster for one pipeline."""
    detail = pipelines_meta_svc.get_pipeline(pipeline)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline}' not found.")
    return detail


@router.get(
    "/pipeline-packages/{pipeline}/plan",
    response_model=PipelinePlanResponse,
    summary="Pipeline run plan: tables, transformation stages, fixers",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_pipeline_plan(pipeline: str) -> PipelinePlanResponse:
    """Return what ``pipeline`` would run under ``--tables all``.

    Powers the submit-page table / transformation picker. Reproduces
    the pipeline's ``resolve_tables`` / ``resolve_transformations``
    expansion without importing the (forbidden) ``ovid2.core`` tier.
    """
    try:
        return pipeline_plan_svc.get_pipeline_plan(pipeline)
    except pipeline_plan_svc.PipelinePlanError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get(
    "/sources/tables",
    response_model=SourceTablesResponse,
    summary="Table roster of a copy/move source (e.g. Access file)",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_source_tables(
    address: str = Query("", description="Saved source-address name (Saved-address mode)."),
    connection: str = Query("", description="Storage-account name (Browse-storage mode)."),
    location: str = Query(
        "",
        description="Browsed source path within the storage account "
        "(the .accdb / .mdb file), for Browse-storage mode.",
    ),
) -> SourceTablesResponse:
    """Enumerate the tables inside a copy/move source.

    Powers the submit-page table picker for ``copy`` / ``move`` jobs:
    their tables live *inside* a self-describing source container (a
    Microsoft Access file) rather than a CDM. Enumeration is delegated to
    the trust-safe storage-browse executor hand-off; ``supported=False``
    for sources that cannot be enumerated this way (the picker then
    offers a free-text list). Pass either ``address`` (saved-address
    mode) or ``connection`` + ``location`` (browse mode).
    """
    if address.strip():
        tables, supported = source_tables_svc.tables_for_address(address)
    elif connection.strip():
        tables, supported = source_tables_svc.tables_for_connection(connection, location)
    else:
        return SourceTablesResponse(
            supported=False,
            tables=[],
            message="Provide an 'address' or a 'connection'.",
        )
    message = ""
    if supported and not tables:
        message = (
            "Source is enumerable but no tables were returned "
            "(unreadable file, or table listing unavailable in this tier)."
        )
    return SourceTablesResponse(supported=supported, tables=tables, message=message)


@router.get(
    "/pipeline-packages/{pipeline}/file",
    response_model=PipelineFilePreview,
    summary="Preview one pipeline file (whitelisted)",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_pipeline_file(
    pipeline: str,
    relative_path: str = Query(..., description="Path relative to the pipeline package."),
) -> PipelineFilePreview:
    """Return the text of a pipeline file after anti-traversal checks."""
    result = pipelines_meta_svc.read_pipeline_file(pipeline, relative_path)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found or not permitted.")
    path, content = result
    size = path.stat().st_size
    truncated = size > len(content)
    return PipelineFilePreview(
        pipeline=pipeline,
        relative_path=relative_path,
        absolute_path=str(path),
        size_bytes=size,
        truncated=truncated,
        content=content,
    )


@router.get(
    "/pipeline-packages/{pipeline}/formatters",
    response_model=list[dict[str, object]],
    summary="List one pipeline's formatter specs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_formatters(pipeline: str) -> list[dict[str, object]]:
    """Return the formatter file listing for a pipeline as a list of dicts."""
    return [f.model_dump() for f in formatters_svc.list_formatters(pipeline)]


@router.get(
    "/pipeline-packages/{pipeline}/formatters/{table}",
    response_model=FormatterSpecDetail,
    summary="Get one formatter spec",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_formatter(pipeline: str, table: str) -> FormatterSpecDetail:
    """Return raw + parsed contents for one formatter spec."""
    detail = formatters_svc.get_formatter(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Formatter '{pipeline}/{table}' not found.",
        )
    return detail


@router.post(
    "/pipeline-packages/{pipeline}/formatters:validate",
    response_model=FormatterValidationReport,
    summary="Validate every formatter YAML for a pipeline",
    # capability: schema-validate is part of the config-authoring workflow;
    # gate with the config-write tier (more restrictive than read).
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_validate_formatters(pipeline: str) -> FormatterValidationReport:
    """Run the JSON-schema validator against every YAML spec."""
    return formatters_svc.validate_pipeline(pipeline)


@router.put(
    "/pipeline-packages/{pipeline}/formatters/{table}",
    response_model=WriteResult,
    summary="Create or update a formatter YAML spec",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_formatter(
    pipeline: str,
    table: str,
    payload: FormatterWrite,
) -> WriteResult:
    """Write a formatter YAML spec after schema validation."""
    try:
        return formatters_svc.save_formatter(pipeline, table, payload, actor="web")
    except formatters_svc.FormatterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/pipeline-packages/{pipeline}/formatters/{table}",
    response_model=WriteResult,
    summary="Delete a formatter YAML spec",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_formatter(pipeline: str, table: str) -> WriteResult:
    """Remove a formatter YAML spec (Python-dict specs are never touched)."""
    try:
        return formatters_svc.delete_formatter(pipeline, table, actor="web")
    except formatters_svc.FormatterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/pipeline-packages/{pipeline}/mappers",
    response_model=list[dict[str, object]],
    summary="List one pipeline's mapper specs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_mappers(pipeline: str) -> list[dict[str, object]]:
    """Return the mapper file listing for a pipeline as a list of dicts."""
    return [m.model_dump() for m in mappers_svc.list_mappers(pipeline)]


@router.get(
    "/pipeline-packages/{pipeline}/mappers/{table}",
    response_model=MapperSpecDetail,
    summary="Get one mapper spec",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_mapper(pipeline: str, table: str) -> MapperSpecDetail:
    """Return raw + parsed contents for one mapper spec."""
    detail = mappers_svc.get_mapper(pipeline, table)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Mapper '{pipeline}/{table}' not found.",
        )
    return detail


@router.post(
    "/pipeline-packages/{pipeline}/mappers:validate",
    response_model=MapperValidationReport,
    summary="Validate every mapper YAML for a pipeline",
    # capability: schema-validate is part of the config-authoring workflow;
    # gate with the config-write tier (more restrictive than read).
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_validate_mappers(pipeline: str) -> MapperValidationReport:
    """Run the JSON-schema validator against every YAML mapper spec."""
    return mappers_svc.validate_pipeline(pipeline)


@router.put(
    "/pipeline-packages/{pipeline}/mappers/{table}",
    response_model=WriteResult,
    summary="Create or update a mapper YAML spec",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_mapper(
    pipeline: str,
    table: str,
    payload: MapperWrite,
) -> WriteResult:
    """Write a mapper YAML spec after schema validation."""
    try:
        return mappers_svc.save_mapper(pipeline, table, payload, actor="web")
    except mappers_svc.MapperError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/pipeline-packages/{pipeline}/mappers/{table}",
    response_model=WriteResult,
    summary="Delete a mapper YAML spec",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_mapper(pipeline: str, table: str) -> WriteResult:
    """Remove a mapper YAML spec (Python ``SPEC`` modules are never touched)."""
    try:
        return mappers_svc.delete_mapper(pipeline, table, actor="web")
    except mappers_svc.MapperError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/runs",
    response_model=RunListResponse,
    summary="Paginated run history",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_list_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Exact-match status filter."),
    pipeline: str | None = Query(None, description="Pipeline name filter."),
) -> RunListResponse:
    """Return a paginated slice of the run history."""
    return runs_svc.list_runs(limit=limit, offset=offset, status=status, pipeline=pipeline)


@router.get(
    "/runs/{batch_id}",
    response_model=RunSummary,
    summary="One run by batch_id",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_get_run(batch_id: str) -> RunSummary:
    """Return the run row for one ``batch_id``."""
    run = runs_svc.get_run(batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{batch_id}' not found.")
    return run


@router.get(
    "/runs/{instance_id}/events",
    response_model=RunEventPage,
    summary="Cursor-paged events for a run",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_events(
    instance_id: str,
    after: int = Query(0, ge=0, description="Byte-offset cursor returned by the previous call."),
    limit: int = Query(RUN_EVENTS_PAGE_SIZE, ge=1, le=5000),
) -> RunEventPage:
    """Return the JSONL events appended after the given cursor."""
    return events_svc.read_page(instance_id, after=after, limit=limit)


@router.get(
    "/runs/{instance_id}/timeline",
    response_model=RunTimeline,
    summary="Aggregate stage / table / fix timeline for a run",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_timeline(instance_id: str) -> RunTimeline:
    """Return an aggregated view of the full event log."""
    return timeline_svc.build_timeline(instance_id)


@router.get(
    "/runs/{instance_id}/events.log",
    response_class=PlainTextResponse,
    summary="Raw JSONL event log for one run",
    responses={200: {"content": {"text/plain": {}}}},
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_events_log(instance_id: str) -> PlainTextResponse:
    """Return the full JSONL event log verbatim (one event per line)."""
    path = events_svc.event_log_path(instance_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Event log for '{instance_id}' not found.")
    text = path.read_text(encoding="utf-8", errors="replace")
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.get(
    "/health/checks",
    summary="Registered data-health check catalogue",
    response_model=list,
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_health_checks() -> list[dict[str, Any]]:
    """Return the registered data-health / EDC check catalogue.

    Reads the packaged YAML specs directly (web-safe); does not import
    pipeline code. Each entry carries id / name / part / operator /
    description / edc_table / enabled.
    """
    return health_report_svc.list_checks()


@router.get(
    "/health/packages",
    summary="Registered EDC/DC report-cycle package catalogue",
    response_model=list,
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_health_packages() -> list[dict[str, Any]]:
    """Return the registered EDC/DC report-cycle package catalogue.

    Reads the packaged package manifests directly (web-safe); does not
    import pipeline code. Each entry carries id / label / released /
    description / checks, most-recent first (the first entry is the
    picker default).
    """
    return health_report_svc.list_packages()


@router.get(
    "/transforms",
    summary="Public transformations library catalogue",
    response_model=list[TransformSummary],
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_list_transforms(
    category: str | None = Query(
        default=None,
        description="Filter by category: formatting / correction / reshape / sampling.",
    ),
) -> list[TransformSummary]:
    """Return the CDM-agnostic public transformations catalogue.

    Reads the web-safe catalogue mirror
    (:mod:`ovid2.config.transforms_catalogue`); does not import pipeline
    code. Each entry carries name / summary / category / requires_family
    / params_schema so a user or Agent Ovid can pick and configure a
    transform before composing it into a chain.
    """
    return [TransformSummary(**entry) for entry in transforms_svc.list_transforms(category)]


@router.post(
    "/transforms:validate",
    summary="Validate a candidate transform spec",
    response_model=TransformValidateReport,
    # Schema-validate is part of the config-authoring workflow; gate with
    # the config-write tier (more restrictive than read).
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_validate_transform(payload: TransformValidateRequest) -> TransformValidateReport:
    """Validate a transform spec against the schema and the op catalogue.

    Lets an agent catch an unknown op or malformed step *before* saving
    the spec. Returns ``{ok, errors}``.
    """
    result = transforms_svc.validate_transform_spec(payload.spec)
    return TransformValidateReport(ok=bool(result["ok"]), errors=list(result["errors"]))


@router.get(
    "/runs/{instance_id}/report",
    summary="Data-health report artifact for a run",
    response_model=dict,
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_health_report(instance_id: str) -> dict[str, Any]:
    """Return the parsed data-health report artifact for ``instance_id``.

    The artifact is written by ``ovid2 health`` beside the event log
    (``health-report-<instance_id>.json``). Shape validated against
    ``resources/schemas/health_report.v1.json``. 404 when the run was
    not a health run (or the artifact hasn't been written yet).
    """
    report = health_report_svc.get_report(instance_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"No health report for run '{instance_id}'.")
    return report


# ---------------------------------------------------------------------------
# Live engine metrics
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{instance_id}/metrics/latest",
    summary="Most recent engine.metrics sample for a run",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_metrics_latest(instance_id: str) -> dict[str, Any]:
    """Return the most recent live-metrics sample for the run.

    Returns ``{}`` when no sample has been written yet — the sampler
    ticks a few hundred milliseconds after the session comes up, so
    early polls during job start-up will legitimately see an empty
    payload for a moment.
    """
    from ovid2.web.services import metrics_svc

    record = metrics_svc.latest(instance_id)
    return record if record is not None else {}


@router.get(
    "/runs/{instance_id}/metrics/stream",
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_run_metrics_stream(instance_id: str) -> StreamingResponse:
    """Server-Sent Events stream of live ``engine.metrics`` samples.

    Frames:

    * ``event: sample`` — a new ``engine.metrics`` record.
    * ``event: finished`` — the run's ``instance.finished`` was
      observed; the stream is about to close.
    * SSE comments (``: keepalive``) every ``LIVE_SSE_KEEPALIVE_S``
      seconds.

    Not part of the OpenAPI snapshot because SSE isn't JSON.
    """
    from fastapi.responses import StreamingResponse

    from ovid2.web.services import metrics_svc

    return StreamingResponse(
        metrics_svc.stream(instance_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@router.get(
    "/briefing/cooking",
    summary="Live briefing of runs currently cooking",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_briefing_cooking() -> list[dict[str, Any]]:
    """Return a compact list of runs currently running + their last sample.

    Powers the "cooking now" one-liner on the dashboard. Reads
    ``web_jobs`` for status='running' and joins each with the latest
    ``engine.metrics`` sample. Returns ``[]`` when nothing is cooking
    — the dashboard renders nothing in that case.
    """
    from ovid2.web.services import metrics_svc

    return metrics_svc.briefing_cooking()


# ---------------------------------------------------------------------------
# Jobs (W3)
# ---------------------------------------------------------------------------


@router.post(
    "/runs",
    response_model=JobSubmitResponse,
    status_code=201,
    summary="Submit a new ETL run",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_submit_run(request: JobSubmitRequest) -> JobSubmitResponse:
    """Enqueue an ETL job. The worker will spawn ``ovid2 etl`` shortly."""
    try:
        return jobs_svc.submit(request, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/jobs",
    response_model=JobListResponse,
    summary="List web-tier jobs",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_list_jobs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Exact-match status filter."),
    pipeline: str | None = Query(None, description="Pipeline name filter."),
) -> JobListResponse:
    """Return a paginated slice of the ``web_jobs`` table."""
    try:
        return jobs_svc.list_jobs(limit=limit, offset=offset, status=status, pipeline=pipeline)
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetail,
    summary="Get one job by id",
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_get_job(job_id: str) -> JobDetail:
    """Return one job row, or 404 when unknown."""
    detail = jobs_svc.get_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return detail


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobDetail,
    summary="Cancel a job",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_cancel_job(job_id: str) -> JobDetail:
    """Request cancellation of a queued or running job."""
    try:
        return jobs_svc.cancel(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/jobs/{job_id}/rerun",
    response_model=JobSubmitResponse,
    status_code=201,
    summary="Rerun a finished job",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_rerun_job(job_id: str) -> JobSubmitResponse:
    """Enqueue a byte-identical retry of a finished job."""
    try:
        return jobs_svc.rerun(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/job-drafts",
    response_model=JobSubmitResponse,
    status_code=201,
    summary="Create a draft job (fully specified, not queued)",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_create_job_draft(request: JobSubmitRequest) -> JobSubmitResponse:
    """Validate and store a job as ``draft``.

    Same payload and validation as ``POST /runs``, but the row is never
    claimed by the worker until a human launches it
    (``POST /jobs/{job_id}/launch`` — the ▶ on ``/runs/{instance_id}``).
    """
    try:
        return jobs_svc.create_draft(request, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/jobs/{job_id}/launch",
    response_model=JobDetail,
    summary="Launch a draft job (draft → queued)",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_launch_job(job_id: str) -> JobDetail:
    """Promote a draft job to ``queued`` so the worker runs it."""
    try:
        return jobs_svc.launch_draft(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/jobs/{job_id}/discard",
    status_code=204,
    summary="Discard a draft job",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_discard_job_draft(job_id: str) -> Response:
    """Delete a draft job. Non-draft jobs are refused with 400."""
    try:
        jobs_svc.discard_draft(job_id, actor="web")
    except jobs_svc.SubmitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get(
    "/jobs/{job_id}/log",
    response_class=PlainTextResponse,
    summary="Captured stderr for one job",
    responses={200: {"content": {"text/plain": {}}}},
    dependencies=[Depends(require_capability(Capability.VIEW_RUNS))],
)
def api_job_log(job_id: str) -> PlainTextResponse:
    """Return the subprocess log captured by the worker."""
    if jobs_svc.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    path = jobs_svc.worker_log_path(job_id)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Install wizard (W5)
# ---------------------------------------------------------------------------


@router.get(
    "/install/status",
    response_model=InstallStatus,
    summary="Aggregate install-wizard state (derived live from disk)",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_status() -> InstallStatus:
    """Return the wizard's step-by-step status."""
    return install_svc.compute_status()


@router.post(
    "/install/bootstrap",
    response_model=BootstrapResponse,
    summary="Create the XDG tree and drop sample configs (idempotent)",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_bootstrap(_payload: BootstrapRequest | None = None) -> BootstrapResponse:
    """Bootstrap the XDG directory tree (equivalent to ``ovid2 init``)."""
    log = install_svc.run_bootstrap(actor="web")
    return BootstrapResponse(log=log)


@router.post(
    "/install/snowflake",
    response_model=SnowflakeSaveResponse,
    summary="Persist the Snowflake secret (0o600 enforced)",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_snowflake(payload: SecretWrite) -> SnowflakeSaveResponse:
    """Write ``secrets.d/snowflake.json`` after checking required keys."""
    try:
        result = install_svc.save_snowflake_secret(payload, actor="web")
    except install_svc.InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SnowflakeSaveResponse(path=str(result["path"]), action=str(result["action"]))


@router.post(
    "/install/image/build",
    response_model=ImageBuildDetail,
    status_code=201,
    summary="Start a ``docker build`` in the background",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_image_build(payload: ImageBuildRequest) -> ImageBuildDetail:
    """Kick off a background docker build."""
    try:
        return install_svc.start_image_build(
            tag=payload.tag,
            context=payload.context,
            dockerfile=payload.dockerfile,
            actor="web",
        )
    except install_svc.InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/install/image/builds",
    response_model=ImageBuildListResponse,
    summary="List image-build history",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_image_builds(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ImageBuildListResponse:
    """Return a paginated slice of ``web_image_builds``."""
    return install_svc.list_image_builds(limit=limit, offset=offset)


@router.get(
    "/install/image/builds/{build_id}",
    response_model=ImageBuildDetail,
    summary="Get one image-build row",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_image_build_detail(build_id: str) -> ImageBuildDetail:
    """Return one image-build row or 404."""
    detail = install_svc.get_image_build(build_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Image build '{build_id}' not found.")
    return detail


@router.get(
    "/install/image/builds/{build_id}/log",
    response_class=PlainTextResponse,
    summary="Captured docker-build stdout/stderr",
    responses={200: {"content": {"text/plain": {}}}},
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_image_build_log(build_id: str) -> PlainTextResponse:
    """Return the log tail of one image build."""
    if install_svc.get_image_build(build_id) is None:
        raise HTTPException(status_code=404, detail=f"Image build '{build_id}' not found.")
    path = install_svc.image_build_log_path(build_id)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.get(
    "/install/service/command",
    response_model=InstallServiceCommand,
    summary="Get the sudo command for installing the systemd unit",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_service_command(
    user: str | None = Query(None, description="Override the Unix user."),
    ovid2_bin: str | None = Query(None, description="Override the ``ovid2`` binary path."),
) -> InstallServiceCommand:
    """Return the exact ``sudo ovid2 service install ...`` command."""
    return install_svc.get_service_command(user=user, ovid2_bin=ovid2_bin)


@router.post(
    "/install/pipeline/verify",
    response_model=PipelineVerifyResponse,
    summary="Verify one pipeline package resolves",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_install_pipeline_verify(
    name: str = Query(..., description="Pipeline package name."),
) -> PipelineVerifyResponse:
    """Confirm the requested pipeline package is importable and populated."""
    try:
        meta = install_svc.verify_pipeline(name)
    except install_svc.InstallError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PipelineVerifyResponse(**meta)


# ---------------------------------------------------------------------------
# Connections (W7)
# ---------------------------------------------------------------------------


@router.get(
    "/connections/types",
    response_model=list[ConnectionTypeSpec],
    summary="List registered connector types and their field specs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_connection_types() -> list[ConnectionTypeSpec]:
    """Return the field-spec catalogue for every registered connector."""
    return connections_svc.list_connection_types()


@router.get(
    "/connections",
    response_model=list[ConnectionSummary],
    summary="List connection configs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_connections() -> list[ConnectionSummary]:
    """Return every JSON connection config discoverable via the XDG search path."""
    return connections_svc.list_connections()


@router.get(
    "/connections/{name}",
    response_model=ConnectionDetail,
    summary="Get one connection (masked secrets)",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_connection(name: str) -> ConnectionDetail:
    """Return the parsed contents of one connection config (secrets masked)."""
    detail = connections_svc.get_connection(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Connection '{name}' not found.")
    return detail


@router.post(
    "/connections/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create a connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_create_connection(name: str, payload: ConnectionWrite) -> WriteResult:
    """Create a new connection JSON (secrets land in secrets.d/, mode 0o600)."""
    try:
        return connections_svc.write_connection(name, payload, actor="web", create_only=True)
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/connections/{name}",
    response_model=WriteResult,
    summary="Create or update a connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_connection(name: str, payload: ConnectionWrite) -> WriteResult:
    """Idempotent create-or-update of a connection JSON."""
    try:
        return connections_svc.write_connection(name, payload, actor="web")
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/connections/{name}",
    response_model=WriteResult,
    summary="Delete a connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_connection(name: str) -> WriteResult:
    """Remove a connection JSON from the user-scoped connections dir."""
    _gate("connection.delete", name)
    try:
        return connections_svc.delete_connection(name, actor="web")
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/connections/{name}/test",
    response_model=ConnectionTestReport,
    summary="Probe a connection with a cheap round-trip",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_test_connection(name: str) -> ConnectionTestReport:
    """Attempt a lightweight round-trip probe on the named connection."""
    try:
        return connections_svc.test_connection(name)
    except connections_svc.ConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/storage/{name}/browse",
    response_model=StorageBrowseResponse,
    summary="Browse a storage account's folder tree",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_browse_storage(name: str, prefix: str | None = Query(None)) -> StorageBrowseResponse:
    """List one level of a storage account's contents.

    Drives the graphical folder/pipeline picker on the pipeline submit
    page. ``dir`` entries are navigable (pass ``entry.path`` back as
    ``prefix``); ``file`` / ``table`` entries are leaves. Returns
    ``supported=False`` when the storage type has no browse capability.
    """
    try:
        return storage_browse_svc.browse_storage(name, prefix)
    except storage_browse_svc.StorageBrowseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Pipelines (W7)
# ---------------------------------------------------------------------------


@router.get(
    "/pipelines",
    response_model=list[PipelineConfigSummary],
    summary="List pipelines",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_pipeline_configs() -> list[PipelineConfigSummary]:
    """Return every pipeline JSON discoverable via the XDG search path."""
    return pipelines_svc.list_pipelines()


@router.get(
    "/pipelines/{name}",
    response_model=PipelineConfigDetail,
    summary="Get one pipeline (with runtime state)",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_pipeline_config(name: str) -> PipelineConfigDetail:
    """Return one pipeline detail, including next_fire_at."""
    detail = pipelines_svc.get_pipeline(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{name}' not found.")
    return detail


@router.post(
    "/pipelines/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create a pipeline",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_create_pipeline_config(name: str, payload: PipelineWrite) -> WriteResult:
    """Create a new pipeline JSON."""
    try:
        return pipelines_svc.write_pipeline(name, payload, actor="web", create_only=True)
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/pipelines/{name}",
    response_model=WriteResult,
    summary="Create or update a pipeline",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_pipeline_config(name: str, payload: PipelineWrite) -> WriteResult:
    """Idempotent create-or-update of a pipeline JSON."""
    try:
        return pipelines_svc.write_pipeline(name, payload, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/pipelines/{name}",
    response_model=WriteResult,
    summary="Delete a pipeline",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_pipeline_config(name: str) -> WriteResult:
    """Remove a pipeline JSON."""
    _gate("pipeline.delete", name)
    try:
        return pipelines_svc.delete_pipeline(name, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/pipelines/{name}/draft",
    response_model=JobSubmitResponse,
    status_code=201,
    summary="Create a draft job from a saved pipeline config",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_draft_pipeline_config(
    name: str, body: PipelineRunRequest | None = None
) -> JobSubmitResponse:
    """Like ``POST /pipelines/{name}/run`` but always stores a *draft*.

    The draft carries every field of the saved config plus the one-run
    overrides, and is only executed when a human launches it
    (``POST /jobs/{job_id}/launch``). Used by the assistant's
    ``draft_pipeline_config`` tool.
    """
    body = body or PipelineRunRequest()
    tables = [t for t in body.tables if t and t != "all"] if body.tables is not None else None
    try:
        return pipelines_svc.run_now(
            name,
            actor="web",
            batch_id=(body.batch_id or None),
            input_address=(body.input_address or "").strip() or None,
            output_address=(body.output_address or "").strip() or None,
            tables=tables,
            draft=True,
        )
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/pipelines/{name}/run",
    response_model=JobSubmitResponse,
    status_code=201,
    summary="Submit an ETL job from this pipeline",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_run_pipeline_config(name: str, body: PipelineRunRequest | None = None) -> JobSubmitResponse:
    """Convert a pipeline into a job submission and enqueue it.

    An optional body carries one-run overrides (``batch_id`` /
    ``tables`` / ``input_address`` / ``output_address``); an empty body
    runs the pipeline as configured. Address overrides bind an *open*
    side of the config (e.g. a ``dq_checks`` config whose input is
    chosen per run). ``draft=true`` stores a ready-to-run draft instead
    of queueing. The
    ``tables`` override is how a ``copy`` / ``move`` pipeline created
    *open* on tables supplies the explicit list that ``ovid2 tools
    copy|move`` requires (``['all']`` is still rejected by submit-time
    validation).
    """
    tables_override: list[str] | None = None
    batch_override: str | None = None
    input_override: str | None = None
    output_override: str | None = None
    draft = False
    if body is not None:
        batch_override = (body.batch_id or None) if body.batch_id else None
        input_override = (body.input_address or "").strip() or None
        output_override = (body.output_address or "").strip() or None
        draft = bool(body.draft)
        if body.tables is not None:
            # Drop any stray ``all`` token — copy / move have no ``all``
            # sentinel. An explicitly-supplied list (even empty after the
            # scrub) is a deliberate override; ``None`` keeps stored tables.
            tables_override = [t for t in body.tables if t and t != "all"]
    try:
        return pipelines_svc.run_now(
            name,
            actor="web",
            batch_id=batch_override,
            input_address=input_override,
            output_address=output_override,
            tables=tables_override,
            draft=draft,
        )
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/pipelines/{name}/enable",
    response_model=PipelineConfigDetail,
    summary="Enable a pipeline (trigger will fire)",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_enable_pipeline_config(name: str) -> PipelineConfigDetail:
    """Set enabled=true on the named pipeline."""
    try:
        return pipelines_svc.set_enabled(name, True, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/pipelines/{name}/disable",
    response_model=PipelineConfigDetail,
    summary="Disable a pipeline (trigger will not fire)",
    dependencies=[Depends(require_capability(Capability.SUBMIT_JOBS))],
)
def api_disable_pipeline_config(name: str) -> PipelineConfigDetail:
    """Set enabled=false on the named pipeline."""
    try:
        return pipelines_svc.set_enabled(name, False, actor="web")
    except pipelines_svc.PipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Settings (W7)
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=GlobalSettings,
    summary="Return the global runtime settings",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_settings() -> GlobalSettings:
    """Return the merged settings (disk-backed values + built-in defaults)."""
    return settings_svc.get_settings()


@router.put(
    "/settings",
    response_model=GlobalSettings,
    summary="Persist the global runtime settings",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_put_settings(payload: GlobalSettings) -> GlobalSettings:
    """Overwrite settings.json with the supplied model."""
    try:
        return settings_svc.save_settings(payload, actor="web")
    except settings_svc.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Engines (E1)
# ---------------------------------------------------------------------------


@router.get(
    "/engines/types",
    response_model=list[EngineTypeSpec],
    summary="List registered engine types and their field specs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_engine_types() -> list[EngineTypeSpec]:
    """Return the field-spec catalogue for every registered engine."""
    return engines_svc.list_engine_types()


@router.get(
    "/engines",
    response_model=list[EngineSummary],
    summary="List engine configs",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_list_engines() -> list[EngineSummary]:
    """Return every JSON engine config discoverable via the XDG search path."""
    return engines_svc.list_engines()


@router.get(
    "/engines/{name}",
    response_model=EngineDetail,
    summary="Get one engine (masked secrets)",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_get_engine(name: str) -> EngineDetail:
    """Return the parsed contents of one engine config (secrets masked)."""
    detail = engines_svc.get_engine(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Engine '{name}' not found.")
    return detail


@router.post(
    "/engines/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create an engine",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_create_engine(name: str, payload: EngineWrite) -> WriteResult:
    """Create a new engine JSON (secrets land in secrets.d/, mode 0o600)."""
    try:
        return engines_svc.write_engine(name, payload, actor="web", create_only=True)
    except engines_svc.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/engines/{name}",
    response_model=WriteResult,
    summary="Create or update an engine",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_engine(name: str, payload: EngineWrite) -> WriteResult:
    """Idempotent create-or-update of an engine JSON."""
    try:
        return engines_svc.write_engine(name, payload, actor="web")
    except engines_svc.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/engines/{name}",
    response_model=WriteResult,
    summary="Delete an engine",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_engine(name: str) -> WriteResult:
    """Remove an engine JSON. Refuses when the engine is still referenced."""
    _gate("engine.delete", name)
    try:
        return engines_svc.delete_engine(name, actor="web")
    except engines_svc.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/engines/{name}/test",
    response_model=EngineTestReport,
    summary="Probe an engine with a subprocess ovid2 tools test-engine call",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_test_engine(name: str) -> EngineTestReport:
    """Attempt a lightweight round-trip probe on the named engine."""
    try:
        return engines_svc.test_engine(name)
    except engines_svc.EngineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Ovid Data Space (ODS) — read-only naming layer
# ---------------------------------------------------------------------------


@router.get(
    "/dataspace",
    response_model=OdsPipelineListing,
    summary="List every pipeline in the Ovid Data Space",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_ods_list_pipelines() -> OdsPipelineListing:
    """Return every pipeline and the dimensions they are known in."""
    return dataspace_svc.list_pipelines()


@router.get(
    "/dataspace/dimensions",
    response_model=OdsDimensionCatalog,
    summary="List every ODS dimension + resolution defaults",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_ods_dimensions() -> OdsDimensionCatalog:
    """Return the dimension catalogue and the deployment's routing defaults."""
    return dataspace_svc.get_dimension_catalog()


@router.get(
    "/dataspace/resolve",
    response_model=OdsResolveResult,
    summary="Resolve an ODS path + dimension to an address",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_ods_resolve(
    path: str = Query(..., description="ODS path (e.g. /demo/20260316/DEMOGRAPHIC)."),
    dim: str | None = Query(
        None,
        description="Optional dimension name (disc, snowflake, s3, ...).",
    ),
) -> OdsResolveResult:
    """Return the address that would serve ``(path, dim)`` without doing I/O."""
    try:
        return dataspace_svc.resolve_path(path, dim=dim)
    except dataspace_svc.OdsServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/dataspace/{pipeline}",
    response_model=OdsBatchListing,
    summary="List every batch under a pipeline",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_ods_list_batches(pipeline: str) -> OdsBatchListing:
    """Return the batches under ``/{pipeline}``."""
    try:
        return dataspace_svc.list_pipeline_batches(pipeline)
    except dataspace_svc.OdsServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/dataspace/{pipeline}/{batch_id}",
    response_model=OdsTableListing,
    summary="List every table under a pipeline batch",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_ods_list_tables(pipeline: str, batch_id: str) -> OdsTableListing:
    """Return the tables under ``/{pipeline}/{batch_id}``."""
    try:
        return dataspace_svc.list_batch_tables(pipeline, batch_id)
    except dataspace_svc.OdsServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Filesystem path picker
# ---------------------------------------------------------------------------


@router.get(
    "/fs/roots",
    response_model=FsRoots,
    summary="Curated starting points for the disc path picker",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_fs_roots() -> FsRoots:
    """Return the roots the picker offers as jump-off points."""
    return fs_svc.list_roots()


@router.get(
    "/fs/list",
    response_model=FsListing,
    summary="List one directory on the host filesystem",
    dependencies=[Depends(require_capability(Capability.VIEW_PIPELINE_DATA))],
)
def api_fs_list(
    path: str | None = Query(
        None,
        description=("Absolute host path to list. Empty falls back to the first allowlist root."),
    ),
    show_hidden: bool | None = Query(
        None,
        description=(
            "Include dotfiles / dot-directories. Falls back to the "
            "``fs_picker.show_hidden`` setting when omitted."
        ),
    ),
) -> FsListing:
    """Return a directory listing scoped by ``settings.fs_picker.roots``."""
    try:
        return fs_svc.list_dir(path, show_hidden=show_hidden)
    except fs_svc.FsPickerDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except fs_svc.FsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except fs_svc.FsAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# LLM connections + assistant chat (L1)
# ---------------------------------------------------------------------------


@router.get(
    "/llms/types",
    response_model=list[LlmProviderSpec],
    summary="List LLM providers and their field specs",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_list_llm_types() -> list[LlmProviderSpec]:
    """Return the field-spec catalogue for every LLM provider."""
    return llms_svc.list_providers()


@router.get(
    "/llms",
    response_model=list[LlmSummary],
    summary="List LLM connections",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_list_llms() -> list[LlmSummary]:
    """Return every LLM connection on the XDG search path."""
    return llms_svc.list_llms()


@router.get(
    "/llms/{name}",
    response_model=LlmDetail,
    summary="Get one LLM connection (masked secrets)",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_get_llm(name: str) -> LlmDetail:
    """Return one LLM connection with its API key masked."""
    detail = llms_svc.get_llm(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"LLM connection '{name}' not found.")
    return detail


@router.post(
    "/llms/{name}",
    response_model=WriteResult,
    status_code=201,
    summary="Create an LLM connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_create_llm(name: str, payload: LlmWrite) -> WriteResult:
    """Create an LLM connection (API key lands in secrets.d/, mode 0o600)."""
    try:
        return llms_svc.write_llm(name, payload, actor="web", create_only=True)
    except llms_svc.LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/llms/{name}",
    response_model=WriteResult,
    summary="Create or update an LLM connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_put_llm(name: str, payload: LlmWrite) -> WriteResult:
    """Idempotent create-or-update of an LLM connection."""
    try:
        return llms_svc.write_llm(name, payload, actor="web")
    except llms_svc.LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/llms/{name}",
    response_model=WriteResult,
    summary="Delete an LLM connection",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_delete_llm(name: str) -> WriteResult:
    """Remove an LLM connection. Requires approval; refused while routed to."""
    _gate("llm.delete", name)
    try:
        return llms_svc.delete_llm(name, actor="web")
    except llms_svc.LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/llms/{name}/test",
    response_model=LlmTestReport,
    summary="Probe an LLM connection in an executor subprocess",
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)
def api_test_llm(name: str) -> LlmTestReport:
    """Authenticate against the provider and confirm the model is available."""
    try:
        return llms_svc.test_llm(name)
    except llms_svc.LlmError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/llm/status",
    response_model=LlmStatus,
    summary="Assistant routing status (general + System 1)",
    dependencies=[Depends(require_capability(Capability.USE_LLM))],
)
def api_llm_status() -> LlmStatus:
    """Return whether the assistant is enabled and which models it routes to."""
    return llms_svc.status()


def _loopback_api_url(request: Request) -> str | None:
    """Return the URL the executor child should use to call this server.

    Prefers ``OVID2_LLM_TOOLS_API_URL``; otherwise the listening socket
    (``scope["server"]``), so callbacks never traverse the public proxy.
    """
    if LLM_TOOLS_API_URL:
        return LLM_TOOLS_API_URL.rstrip("/")
    server = request.scope.get("server")
    if not server or not server[0]:
        return None
    host, port = str(server[0]), int(server[1] or 80)
    if host in ("0.0.0.0", "::"):  # noqa: S104 — rewriting a wildcard bind, not binding
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


@router.get(
    "/llm/tools",
    response_model=list[LlmToolInfo],
    summary="Agent tools the assistant may call for the current user",
)
def api_llm_tools(
    ctx: AuthContext = Depends(require_capability(Capability.USE_LLM)),
) -> list[LlmToolInfo]:
    """List the ``agent_tools`` catalogue entries available to the caller.

    GET tools run automatically (read-only, per-turn token); the rest are
    proposals the user confirms in the panel. Empty when
    ``settings.llm.tools_enabled`` is off.
    """
    if not settings_svc.get_settings().llm.tools_enabled:
        return []
    return [LlmToolInfo(**t.to_dict()) for t in llm_chat_svc.allowed_tools(ctx.role)]


@router.post(
    "/llm/chat/stream",
    include_in_schema=False,
    summary="Stream one assistant chat turn as Server-Sent Events",
)
def api_llm_chat_stream(
    payload: LlmChatRequest,
    request: Request,
    ctx: AuthContext = Depends(require_capability(Capability.USE_LLM)),
) -> StreamingResponse:
    """Run one routed chat turn (System 1 first, general fallback) as SSE."""
    if ctx.user_id.startswith("llm:"):
        # The assistant may not start a nested assistant turn.
        raise HTTPException(status_code=403, detail="The assistant cannot chat with itself.")
    principal = llm_chat_svc.ChatPrincipal(
        user_id=ctx.user_id,
        role=ctx.role,
        email=ctx.email,
        api_url=_loopback_api_url(request),
    )
    try:
        gen = llm_chat_svc.sse_stream(payload, actor=ctx.email or ctx.user_id, principal=principal)
    except llm_chat_svc.ChatDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Approvals (destructive-op gate, Phase 3)
# ---------------------------------------------------------------------------


@router.get(
    "/approvals",
    response_model=ApprovalListResponse,
    summary="List approval-gate requests",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_list_approvals(
    status: str | None = Query(
        None, description="Filter by status (pending / approved / denied / consumed)."
    ),
) -> ApprovalListResponse:
    """Return approval rows, newest first, optionally filtered by status."""
    return approvals_svc.list_approvals(status)


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalSummary,
    summary="Fetch one approval request",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_get_approval(approval_id: str) -> ApprovalSummary:
    """Return a single approval row."""
    row = approvals_svc.get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found.")
    return row


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalSummary,
    summary="Approve a pending destructive operation",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_approve(approval_id: str, payload: ApprovalDecision) -> ApprovalSummary:
    """Flip a pending approval to ``approved`` (human-in-the-loop)."""
    try:
        return approvals_svc.decide(approval_id, True, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/approvals/{approval_id}/deny",
    response_model=ApprovalSummary,
    summary="Deny a pending destructive operation",
    dependencies=[Depends(require_capability(Capability.ADMIN_ALL))],
)
def api_deny(approval_id: str, payload: ApprovalDecision) -> ApprovalSummary:
    """Flip a pending approval to ``denied``."""
    try:
        return approvals_svc.decide(approval_id, False, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Users (W6) — user-management (todo 17)
# ---------------------------------------------------------------------------


def _parse_role(role: str) -> Role:
    """Parse an incoming role string into a :class:`Role`, rejecting pending.

    Args:
        role: The raw role string from a request body.

    Returns:
        The corresponding :class:`Role` member.

    Raises:
        HTTPException: 400 when the string is not a known role, or when it
            resolves to :attr:`Role.PENDING` (which is not assignable).
    """
    try:
        parsed = Role(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown role: {role!r}") from exc
    if parsed == Role.PENDING:
        raise HTTPException(status_code=400, detail="Role 'pending' cannot be assigned.")
    return parsed


@router.get(
    "/users",
    response_model=list[UserSummary],
    summary="List users (optional status filter)",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_list_users(
    status: str | None = Query(
        None,
        description="Filter by status (pending / active / disabled).",
    ),
) -> list[UserSummary]:
    """Return every known user, optionally filtered by ``status``."""
    return [UserSummary(**record) for record in users_svc.list_users(status)]


@router.get(
    "/users/{user_id}",
    response_model=UserDetail,
    summary="Get one user",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_get_user(user_id: str) -> UserDetail:
    """Return the full record for one user, or 404 if unknown."""
    record = users_svc.get_user(user_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
    return UserDetail(**record)


@router.post(
    "/users/{user_id}/activate",
    response_model=UserDetail,
    summary="Activate a user with a role",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_activate_user(user_id: str, payload: UserActivateRequest) -> UserDetail:
    """Activate ``user_id`` with the requested role.

    Returns 400 for an unknown role or ``pending``; 404 when the user does
    not exist; 409 when the operation would violate the last-admin guard.
    """
    role = _parse_role(payload.role)
    if users_svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
    try:
        record = users_svc.activate(user_id, role, actor="web")
    except users_svc.UserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserDetail(**record)


@router.put(
    "/users/{user_id}/role",
    response_model=UserDetail,
    summary="Change a user's role",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_set_user_role(user_id: str, payload: UserRoleRequest) -> UserDetail:
    """Change the role of ``user_id``.

    Returns 400 for an unknown role or ``pending``; 404 when the user does
    not exist; 409 when the operation would violate the last-admin guard.
    """
    role = _parse_role(payload.role)
    if users_svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
    try:
        record = users_svc.set_role(user_id, role, actor="web")
    except users_svc.UserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserDetail(**record)


@router.post(
    "/users/{user_id}/disable",
    response_model=UserDetail,
    summary="Disable a user",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_disable_user(user_id: str) -> UserDetail:
    """Disable ``user_id``.

    Returns 404 when the user does not exist; 409 when disabling would
    remove the last active system administrator.
    """
    if users_svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
    try:
        record = users_svc.disable(user_id, actor="web")
    except users_svc.UserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserDetail(**record)


@router.post(
    "/users/{user_id}/enable",
    response_model=UserDetail,
    summary="Enable a disabled user",
    dependencies=[Depends(require_capability(Capability.MANAGE_USERS))],
)
def api_enable_user(user_id: str) -> UserDetail:
    """Enable ``user_id`` (sets status to ``active``).

    Returns 404 when the user does not exist.
    """
    if users_svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
    try:
        record = users_svc.enable(user_id, actor="web")
    except users_svc.UserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserDetail(**record)
