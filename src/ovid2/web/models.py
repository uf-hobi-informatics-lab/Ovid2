"""Pydantic models shared by REST routes and MCP tools.

Every ``/api/v1/*`` endpoint's request and response body has a model
here. Keeping them in one place lets the MCP server derive its tool
JSON-schemas from the same source of truth that FastAPI publishes at
``/openapi.json``.

Only W1 read-side models are defined for now — later phases add write
models (``FormatterUpdate``, ``AddressCreate``, …) alongside their
routes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Doctor / health
# ---------------------------------------------------------------------------


class CheckReport(BaseModel):
    """One line of the doctor report."""

    name: str = Field(..., description="Short check identifier (e.g. ``docker``).")
    severity: str = Field(..., description="One of ``ok``, ``warning``, ``error``.")
    message: str = Field(..., description="Human-readable summary.")
    hint: str | None = Field(None, description="Optional remediation hint.")


class HealthResponse(BaseModel):
    """Aggregate output of :func:`ovid2.installer.doctor.run_all_checks`."""

    worst: str = Field(..., description="Highest severity seen across checks.")
    checks: list[CheckReport] = Field(..., description="Per-check results in run order.")


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


class AddressSummary(BaseModel):
    """Lightweight address-config listing entry."""

    name: str = Field(..., description="Filename stem of the JSON config.")
    src_name: str = Field(..., description="Address type (``disc`` or ``snowflake``).")
    path: str = Field(..., description="Absolute path to the JSON file on disk.")
    #: Populated for W7 connection-shaped addresses; ``None`` for legacy ones.
    connection: str | None = Field(
        None,
        description="Name of the backing connection, when this address uses the W7 shape.",
    )


class AddressDetail(BaseModel):
    """Address config with masked ``src_args``."""

    name: str
    src_name: str
    path: str
    src_args: dict[str, Any] = Field(
        ...,
        description="Address arguments. Sensitive keys (password, token) are masked.",
    )
    #: Populated for W7 connection-shaped addresses.
    connection: str | None = None
    location: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    """One row from the batch tracker plus web-tier metadata (if present)."""

    batch_id: str
    path: str | None = None
    status: str = Field(..., description="``NEW`` | ``PROCESSING`` | ``PROCESSED`` | ``ERROR``.")
    received_time: str | None = None
    processing_start: str | None = None
    processing_end: str | None = None
    #: Populated when the run was submitted through the web tier.
    job_id: str | None = None
    instance_id: str | None = None
    pipeline: str | None = None
    submitted_by: str | None = None
    #: Whether a data-health report artifact exists for this run's
    #: instance (drives the "view report" link on the runs list).
    report_available: bool = False


class RunListResponse(BaseModel):
    """Paginated list of :class:`RunSummary`."""

    total: int
    limit: int
    offset: int
    runs: list[RunSummary]


class RunEventPage(BaseModel):
    """Cursor-based page of events tailed from ``events-<instance_id>.jsonl``."""

    instance_id: str
    #: Byte offset the next call should pass back as ``after`` to continue.
    next_cursor: int
    #: True when the event log has been rotated / closed (``instance.finished``).
    finished: bool
    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Run timeline (W2)
# ---------------------------------------------------------------------------


class RunStageSummary(BaseModel):
    """Aggregate counters for one pipeline stage within a run."""

    stage: str = Field(..., description="``read``, ``format``, ``deduplicate``, ``map``, ``fix``, ``write``, or ``other``.")
    started: int = 0
    completed: int = 0
    failed: int = 0


class RunTableSummary(BaseModel):
    """Aggregate status of one table processed by the run."""

    table: str
    status: str = Field(..., description="``pending`` | ``running`` | ``completed`` | ``failed``.")
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class RunFixSummary(BaseModel):
    """One fix event surfaced in the timeline."""

    table: str
    fix: str
    status: str = Field(..., description="``applied`` | ``skipped`` | ``failed``.")
    error: str | None = None


class RunCheckSummary(BaseModel):
    """Aggregate status of one data-health check within a ``health`` run.

    Drives the run window's left pane (check list) and the report viewer
    nav tree. Populated from ``check.started`` / ``check.completed`` /
    ``check.failed`` events.
    """

    check_id: str
    check_name: str = ""
    part: str = ""
    status: str = Field(
        "running", description="``running`` | ``passed`` | ``exception`` | ``error``."
    )
    exception_count: int = 0
    metric_count: int = 0
    error: str | None = None


class RunTimeline(BaseModel):
    """Full aggregation of a run's JSONL event log."""

    instance_id: str
    started_at: str | None = None
    ended_at: str | None = None
    success: bool | None = None
    events_seen: int = 0
    warning_count: int = 0
    error_count: int = 0
    stage_counts: dict[str, RunStageSummary] = Field(default_factory=dict)
    tables: list[RunTableSummary] = Field(default_factory=list)
    fixes: list[RunFixSummary] = Field(default_factory=list)
    checks: list[RunCheckSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipelines (W2)
# ---------------------------------------------------------------------------


class PipelineFileInfo(BaseModel):
    """File metadata surfaced in the pipeline detail view."""

    path: str
    name: str
    size_bytes: int
    line_count: int
    mtime: str = Field(..., description="ISO-8601 UTC timestamp.")


class PipelineSummary(BaseModel):
    """Lightweight listing entry for the pipelines page."""

    name: str
    mappers_type: str
    default_db: str
    path: str
    formatter_count: int
    mapper_count: int


class PipelineTemplateSummary(BaseModel):
    """One curated pipeline template (Phase 3 scaffolder)."""

    id: str
    source_pipeline: str
    cdm: str
    kind: str
    label: str
    description: str


class ScaffoldPipelineRequest(BaseModel):
    """Payload for scaffolding a new pipeline package from a template."""

    name: str = Field(..., description="Name for the new pipeline package.")
    template: str | None = Field(
        None, description="Pipeline template id (from the catalogue)."
    )
    source_pipeline: str | None = Field(
        None,
        description=(
            "Explicit source pipeline to clone. Used with `cdm` as an "
            "alternative to `template`."
        ),
    )
    cdm: str | None = Field(
        None,
        description=(
            "Target CDM name. Required with `source_pipeline`; taken "
            "from the template otherwise."
        ),
    )


class ScaffoldPipelineResponse(BaseModel):
    """Result of a successful pipeline scaffold."""

    name: str
    source_pipeline: str
    cdm: str
    path: str


class StorageBrowseEntry(BaseModel):
    """One entry in a storage-account folder listing (Phase 4)."""

    name: str
    kind: str = Field(..., description="``dir`` | ``file`` | ``table``.")
    path: str = Field(..., description="Round-trippable prefix for the next listing.")


class StorageBrowseResponse(BaseModel):
    """Result of browsing one level of a storage account."""

    storage: str = Field(..., description="Storage-account (connection) name.")
    type: str = Field(..., description="Driver type (disc / s3 / ...).")
    prefix: str = Field("", description="The listed prefix ('' = root).")
    supported: bool = Field(
        True, description="False when this storage type cannot be browsed."
    )
    message: str | None = Field(
        None, description="Human-facing note when not supported / on error."
    )
    entries: list[StorageBrowseEntry] = Field(default_factory=list)


class PipelineDetail(BaseModel):
    """Full read-only detail for one pipeline package."""

    name: str
    path: str
    settings: dict[str, Any]
    formatters: list[PipelineFileInfo]
    mappers: list[PipelineFileInfo]
    pipeline_settings_file: PipelineFileInfo | None = None
    dictionaries_file: PipelineFileInfo | None = None
    fixer_configs_file: PipelineFileInfo | None = None


class TablePlanEntry(BaseModel):
    """One CDM table in a pipeline's run plan (submit-page table picker)."""

    name: str = Field(..., description="CDM table name (formatter stem).")
    has_formatter: bool = Field(
        ...,
        description=(
            "True when a formatter can be resolved for this "
            "(pipeline, table) — mirrors core.loaders.has_formatter."
        ),
    )
    source: str = Field(
        "",
        description=(
            "Which probe matched: 'yaml' | 'python' | 'custom' | '' "
            "(no formatter)."
        ),
    )


class FixerPlanEntry(BaseModel):
    """One fixer in a table's fix chain, surfaced read-only on submit."""

    fix: str = Field(..., description="Registered fixer name.")
    detail: str = Field(
        "", description="Compact human-facing summary of the fixer's args."
    )


class PipelinePlanResponse(BaseModel):
    """What a pipeline would run: tables, transformation stages, fixers.

    Backs ``GET /api/v1/pipelines/{pipeline}/plan`` — the submit-page
    table/transformation picker. Reproduces the ``--tables all`` /
    ``--transformations all`` expansion the pipeline performs at run
    time, without importing the (forbidden) ``ovid2.core`` tier.
    """

    pipeline: str
    cdm: str = Field("", description="Resolved CDM name for the pipeline.")
    tables: list[TablePlanEntry] = Field(default_factory=list)
    transformations: list[str] = Field(
        default_factory=list,
        description="Canonical ETL stages: format, deduplicate, map, fix.",
    )
    fixers: dict[str, list[FixerPlanEntry]] = Field(
        default_factory=dict,
        description="Per-table fix chains (read-only display).",
    )
    multi_table_fixers: list[FixerPlanEntry] = Field(
        default_factory=list,
        description="Cross-table fixes applied once per pipeline.",
    )


class SourceTablesResponse(BaseModel):
    """The concrete table roster of a copy/move *source*.

    Backs ``GET /api/v1/sources/tables`` — the submit-page table picker
    for ``copy`` / ``move`` jobs, whose tables live inside a
    self-describing source container (e.g. a Microsoft Access file)
    rather than a CDM. ``supported`` is ``False`` for sources that are
    not enumerable this way (a plain disc folder, a Snowflake schema),
    signalling the picker to fall back to a free-text list.
    """

    supported: bool = Field(
        False,
        description=(
            "True when the source is a self-describing table container "
            "whose tables were enumerated (e.g. Access)."
        ),
    )
    tables: list[str] = Field(
        default_factory=list,
        description="User-table names inside the source, sorted.",
    )
    message: str = Field(
        "",
        description="Optional note (e.g. why enumeration returned empty).",
    )


class PipelineFilePreview(BaseModel):
    """Text preview of one pipeline file (bounded)."""

    pipeline: str
    relative_path: str
    absolute_path: str
    size_bytes: int
    truncated: bool
    content: str


# ---------------------------------------------------------------------------
# Formatter / mapper specs (W2)
# ---------------------------------------------------------------------------


class FormatterSpecDetail(BaseModel):
    """One formatter spec — raw text plus parsed YAML when available."""

    pipeline: str
    table: str
    path: str
    source_kind: str = Field(..., description="``yaml`` or ``python``.")
    adapter: str | None = None
    raw_text: str
    parsed: dict[str, Any] | None = None


class FormatterValidationEntry(BaseModel):
    """Per-table validation result."""

    table: str
    source_kind: str
    status: str = Field(..., description="``ok`` | ``error`` | ``skipped``.")
    message: str


class FormatterValidationReport(BaseModel):
    """Aggregate validation report for one pipeline's formatters."""

    pipeline: str
    entries: list[FormatterValidationEntry]


class MapperSpecDetail(BaseModel):
    """One mapper spec — raw text plus parsed YAML when available."""

    pipeline: str
    table: str
    path: str
    source_kind: str = Field(..., description="``yaml`` or ``python``.")
    raw_text: str
    parsed: dict[str, Any] | None = None


class MapperValidationEntry(BaseModel):
    """Per-table validation result for mapper specs."""

    table: str
    source_kind: str
    status: str
    message: str


class MapperValidationReport(BaseModel):
    """Aggregate mapper-validation report for one pipeline."""

    pipeline: str
    entries: list[MapperValidationEntry]


# ---------------------------------------------------------------------------
# Public transformations library (transforms catalogue)
# ---------------------------------------------------------------------------


class TransformSummary(BaseModel):
    """One entry in the public transformations catalogue."""

    name: str = Field(description="Transform identifier, e.g. `to_upper`.")
    summary: str = Field(description="One-line human/agent-readable description.")
    category: str = Field(
        description="Grouping: `formatting`, `correction`, `reshape`, or `sampling`."
    )
    requires_family: str | None = Field(
        default=None,
        description="Compute family required (e.g. `spark`), or null for any family.",
    )
    params_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-schema fragment for this transform's params.",
    )


class TransformValidateRequest(BaseModel):
    """Payload for ``POST /api/v1/transforms:validate``."""

    spec: dict[str, Any] = Field(
        description="Candidate transform spec (v/steps/…) to validate."
    )


class TransformValidateReport(BaseModel):
    """Result of validating a candidate transform spec."""

    ok: bool = Field(description="True when the spec passed all checks.")
    errors: list[str] = Field(
        default_factory=list, description="Human-readable validation errors."
    )


# ---------------------------------------------------------------------------
# Job queue (W3)
# ---------------------------------------------------------------------------


class JobSubmitRequest(BaseModel):
    """Payload for ``POST /api/v1/runs`` — one ETL invocation."""

    kind: str = Field(
        "etl",
        description=(
            "Job kind. One of `etl`, `analysis`, `dq_checks`, `copy`, "
            "`move`. Determines which CLI the worker spawns: `etl` → "
            "`ovid2 etl`, `analysis` → `ovid2 analysis`, `dq_checks` "
            "→ `ovid2 dq_checks`, `copy` → `ovid2 tools copy` "
            "(address-to-address one-shot, bypasses formatters/"
            "mappers/fixers), `move` → `ovid2 tools move` (driver-"
            "native successor to `copy`, adds `strict_types` support). "
            "The deprecated alias `cdm` is accepted and normalised to "
            "`etl`."
        ),
    )
    cdm: str | None = Field(
        None,
        description="CDM name, required when kind=etl.",
    )
    pipeline: str = Field(..., description="Pipeline name (must exist under ovid2_pipelines).")
    batch_id: str = Field(..., description="Batch identifier, e.g. ``20260101``.")
    input_address: str = Field(
        "",
        description=(
            "Filename stem of the input address config. Optional when "
            "``input_connection`` (+ ``input_location``) is supplied to "
            "select the database/schema at run time — used by the reusable "
            "data-health / EDC surfaces to target any database without a "
            "saved address."
        ),
    )
    output_address: str = Field(
        "",
        description=(
            "Filename stem of the output address config. Optional when "
            "``output_connection`` (+ ``output_location``) is supplied."
        ),
    )
    input_connection: str = Field(
        "",
        description=(
            "Storage-account (connection) name for the input. When set, an "
            "address is resolved-or-created from ``{connection, location}`` "
            "at submit time (mirrors the /submit 'Browse storage' picker)."
        ),
    )
    input_location: str = Field(
        "",
        description=(
            "Location under ``input_connection`` — e.g. ``DATABASE/SCHEMA`` "
            "for Snowflake or a folder path for object stores."
        ),
    )
    output_connection: str = Field(
        "",
        description="Storage-account (connection) name for the output.",
    )
    output_location: str = Field(
        "",
        description="Location under ``output_connection`` — e.g. ``DATABASE/SCHEMA``.",
    )
    tables: list[str] = Field(
        default_factory=lambda: ["all"],
        description=(
            "Tables to process, or ``['all']`` (etl / analysis / "
            "dq_checks only). For ``kind='copy'`` and ``kind='move'`` "
            "each entry is either a bare logical table name "
            "(``HASH_TOKEN``) or a ``LOGICAL=SOURCE`` split "
            "(``HASH_TOKEN=*datavant_person_tokens*``). LOGICAL is "
            "always used verbatim as the destination file stem — "
            "case preserved — so a disc glob on the source side "
            "never leaks into the output key. SOURCE, when omitted, "
            "defaults to LOGICAL."
        ),
    )
    transformations: list[str] = Field(
        default_factory=lambda: ["all"],
        description="Transformations to run, or ``['all']``.",
    )
    engine: str = Field("pyspark", description="Compute engine (default: pyspark).")
    workers: int = Field(25, ge=1, le=200, description="Spark worker count for isolated cluster.")
    sf_database: str | None = Field(None, description="Snowflake DATABASE override.")
    sf_account: str | None = Field(None, description="Snowflake ACCOUNT override.")
    sf_schema: str | None = Field(None, description="Snowflake SCHEMA override.")
    stderr_log: bool = Field(
        False, description="Mirror events to stderr as plain-text log lines."
    )
    extra_mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Additional bind mounts for the per-invocation container "
            "(``kind='copy'`` and ``kind='move'`` only). Each entry "
            "has the form ``HOST:CONTAINER[:MODE]`` where MODE is "
            "``rw`` (default) or ``ro``. The container path must not "
            "shadow the standard mount set (``/app``, ``/data``, "
            "``/ovid2-user-config``, ``/ovid_credentials``, "
            "``/usr/include/rsa``). Ignored for every non-copy / "
            "non-move kind; ``jobs_svc._validate_submit`` rejects the "
            "field on those kinds so a mis-authored payload can't "
            "smuggle a mount onto an ETL job."
        ),
    )
    strict_types: bool = Field(
        False,
        description=(
            "``kind='move'`` only. Toggle the D6.0 advisory CDM type "
            "check between warn (default, ``False``) and raise "
            "(``True``). A source/target address whose stamped CDM "
            "disagrees produces a log warning when advisory and "
            "aborts the move when strict. Ignored on every non-move "
            "kind; ``jobs_svc._validate_submit`` rejects the field "
            "when it is ``True`` on any other kind so a mis-authored "
            "payload can't quietly enable it."
        ),
    )
    parts: list[str] = Field(
        default_factory=lambda: ["all"],
        description=(
            "``kind='health'`` only. Check parts to run (``part1``.."
            "``part4``, ``mapping``, ``freshness``, ``integrity``, "
            "``completeness``, ``semantic``) or ``['all']``. Ignored "
            "on every other kind."
        ),
    )
    checks: list[str] = Field(
        default_factory=list,
        description=(
            "``kind='health'`` only. Explicit check ids/names to run "
            "(e.g. ``['1.01', 'DC_3_09']``); overrides ``packages`` and "
            "``parts`` when non-empty. Ignored on every other kind."
        ),
    )
    packages: list[str] = Field(
        default_factory=list,
        description=(
            "``kind='health'`` only. EDC/DC report-cycle packages to run "
            "(e.g. ``['20r1']``); expands to the union of the packages' "
            "member checks. Overrides ``parts``; itself overridden by "
            "``checks``. Ignored on every other kind."
        ),
    )
    prev_input_address: str | None = Field(
        None,
        description=(
            "``kind='health'`` only. Prior-refresh input address for "
            "the freshness / volume-trend panel. Ignored otherwise."
        ),
    )


class JobSubmitResponse(BaseModel):
    """Response body for ``POST /api/v1/runs``."""

    job_id: str
    instance_id: str
    status: str


class JobDetail(BaseModel):
    """One row of the ``web_jobs`` table."""

    job_id: str
    submitted_at: str
    submitted_by: str
    parent_job_id: str | None = None
    pipeline: str
    batch_id: str
    input_address: str
    output_address: str
    tables: list[str]
    transformations: list[str]
    extra_args: dict[str, Any] = Field(default_factory=dict)
    status: str
    cancel_requested: bool = False
    instance_id: str | None = None
    pid: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


class JobListResponse(BaseModel):
    """Paginated ``web_jobs`` listing."""

    total: int
    limit: int
    offset: int
    jobs: list[JobDetail]


# ---------------------------------------------------------------------------
# Config CRUD (W4)
# ---------------------------------------------------------------------------


class AddressWrite(BaseModel):
    """Payload for creating or updating an address JSON config.

    Supports two shapes:

    - **Legacy** (pre-W7): populate ``src_name`` + ``src_args``.
    - **New** (W7): populate ``connection`` + ``location``. The address
      then dereferences the named connection at load time to build the
      run-time ``src_args`` — see :mod:`ovid2.config.addresses`.

    Exactly one shape must be supplied. Mixing them raises validation
    errors on the service side.
    """

    src_name: str | None = Field(
        None,
        description="Legacy shape: address type — ``disc`` or ``snowflake``.",
    )
    src_args: dict[str, Any] | None = Field(
        None,
        description=(
            "Legacy shape: flat address arguments. Sensitive keys sent as "
            "the sentinel string ``***`` are preserved from the on-disk copy."
        ),
    )
    connection: str | None = Field(
        None,
        description="New shape: name of the connection this address views.",
    )
    location: dict[str, Any] | None = Field(
        None,
        description=(
            "New shape: per-address location (path/database/schema/…) merged "
            "with the connection at run time."
        ),
    )


class WriteResult(BaseModel):
    """Common response envelope for successful writes."""

    name: str = Field(..., description="Logical resource name (file stem).")
    path: str = Field(..., description="Absolute path of the written file.")
    action: str = Field(..., description="One of ``created`` | ``updated`` | ``deleted``.")


class ApprovalSummary(BaseModel):
    """One approval-gate row (destructive-op gate, Phase 3)."""

    approval_id: str
    created_at: str
    requested_by: str
    action: str = Field(..., description="Dotted action, e.g. ``secret.delete``.")
    target: str | None = None
    status: str = Field(..., description="``pending`` / ``approved`` / ``denied``.")
    decided_at: str | None = None
    decided_by: str | None = None
    reason: str | None = None


class ApprovalListResponse(BaseModel):
    """Envelope for the approvals index."""

    approvals: list[ApprovalSummary] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    """Payload for approving or denying a pending request."""

    decided_by: str = Field(default="web", description="Who made the decision.")
    reason: str | None = Field(default=None, description="Optional note.")


class SecretSummary(BaseModel):
    """Lightweight listing entry for the secrets index page."""

    name: str = Field(..., description="Filename stem of the secret JSON blob.")
    path: str = Field(..., description="Absolute on-disk path.")
    mode: str = Field(..., description="Octal permission bits, e.g. ``0600``.")
    size_bytes: int
    mtime: str = Field(..., description="ISO-8601 UTC modification time.")
    keys: list[str] = Field(
        default_factory=list,
        description="Top-level JSON keys. Values are never returned.",
    )


class SecretDetail(BaseModel):
    """Structural view of a secret — key names only, never values."""

    name: str
    path: str
    mode: str
    keys: list[str]


class SecretWrite(BaseModel):
    """Payload for creating or updating a secret JSON blob.

    Values may be supplied as strings, numbers, or booleans. The write
    path writes the file with ``0o600`` from the start (``os.open`` with
    ``mode=0o600``) so it is never briefly world-readable.

    Any key with the sentinel value ``"***"`` is treated as "keep the
    existing value from disk" so the UI can safely display a masked view.
    """

    values: dict[str, Any] = Field(
        ...,
        description="Top-level JSON object to persist. Values must be JSON-serialisable scalars.",
    )


class FormatterWrite(BaseModel):
    """Payload for saving a formatter YAML spec.

    ``raw_text`` is the full YAML source. The service validates the
    parsed document against the packaged JSON schema before touching
    disk; the write is refused when validation fails.
    """

    raw_text: str = Field(..., description="Full YAML source (UTF-8).")


class MapperWrite(BaseModel):
    """Payload for saving a mapper YAML spec (JSON-schema validated)."""

    raw_text: str = Field(..., description="Full YAML source (UTF-8).")


# ---------------------------------------------------------------------------
# Install wizard (W5)
# ---------------------------------------------------------------------------


class InstallStepStatus(BaseModel):
    """One row of the install-wizard status table.

    ``status`` is one of ``done``, ``pending``, ``running``, or
    ``warning``. Warnings still count as "complete" — the wizard's
    aggregate ``complete`` flag ignores them — but surface a caveat the
    operator should investigate (e.g. no systemctl on this host).
    """

    key: str = Field(..., description="Machine name — matches the wizard step id.")
    status: str = Field(..., description="``done`` | ``pending`` | ``running`` | ``warning``.")
    summary: str = Field(..., description="One-line human-readable status.")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form JSON-serialisable extras (hashes, paths, hints).",
    )


class InstallStatus(BaseModel):
    """Aggregate wizard state — recomputed on every request."""

    current_step: str = Field(
        ...,
        description="Machine name of the first incomplete step, or ``done``.",
    )
    complete: bool = Field(
        ..., description="True when every step is ``done`` or ``warning``."
    )
    steps: list[InstallStepStatus] = Field(
        ..., description="Ordered list of step statuses (bootstrap first, ``done`` last)."
    )


class BootstrapRequest(BaseModel):
    """Payload for ``POST /api/v1/install/bootstrap``.

    Reserved for future flags (e.g. ``interactive``). Empty in W5 so the
    call is idempotent.
    """


class BootstrapResponse(BaseModel):
    """Response body for ``POST /api/v1/install/bootstrap``."""

    log: list[str] = Field(..., description="Ordered log lines returned by the bootstrap helper.")


class SnowflakeSaveResponse(BaseModel):
    """Response body for ``POST /api/v1/install/snowflake``."""

    path: str = Field(..., description="Absolute path of the written secret file.")
    action: str = Field(..., description="``created`` or ``updated``.")


class ImageBuildRequest(BaseModel):
    """Payload for ``POST /api/v1/install/image/build``."""

    tag: str | None = Field(
        None,
        description="Image tag (defaults to ``ovid2.config.settings.DOCKER_IMAGE``).",
    )
    context: str | None = Field(
        None,
        description="Build-context directory (defaults to the repo-local checkout).",
    )
    dockerfile: str | None = Field(
        None,
        description="Path to a ``Dockerfile`` (defaults to ``<context>/docker/Dockerfile``).",
    )


class ImageBuildDetail(BaseModel):
    """One row of the ``web_image_builds`` table."""

    build_id: str
    submitted_at: str
    submitted_by: str
    tag: str
    context: str
    dockerfile: str
    status: str = Field(
        ..., description="``running`` | ``succeeded`` | ``failed``."
    )
    pid: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    error: str | None = None


class ImageBuildListResponse(BaseModel):
    """Paginated ``web_image_builds`` listing."""

    total: int
    limit: int
    offset: int
    builds: list[ImageBuildDetail]


class InstallServiceCommand(BaseModel):
    """The sudo command the operator should run to install the systemd unit."""

    command: str = Field(..., description="Fully-quoted shell command.")
    user: str = Field(..., description="Unix user the daemon should run as.")
    ovid2_bin: str = Field(..., description="Path to the ``ovid2`` executable.")


class PipelineVerifyResponse(BaseModel):
    """Response body for ``POST /api/v1/install/pipeline/verify``."""

    name: str
    path: str
    formatters: int
    mappers: int


# ---------------------------------------------------------------------------
# Connections (W7)
# ---------------------------------------------------------------------------


class ConnectionFieldSpec(BaseModel):
    """Description of one form field for a connector type.

    Consumed by the web UI to render the dynamic form (only fields
    relevant to the selected connector show up) and by MCP tools to
    validate submissions client-side.

    ``widget`` (optional) hints the renderer at a non-default input
    type. Currently recognised values:

    - ``path_picker`` — text input with a "Browse…" button that opens
      the host-side directory browser (see ``fs_svc``). Used by
      ``disc.host_path``.

    ``multiline`` (optional) renders a ``<textarea>`` instead of an
    ``<input>``. Used by the GCS ``service_account_json`` secret.
    """

    name: str
    required: bool = False
    default: Any | None = None
    help: str = ""
    widget: str | None = None
    multiline: bool = False


class ConnectionTypeSpec(BaseModel):
    """Field spec catalogue entry for one connector type."""

    type: str = Field(..., description="Machine name, e.g. ``snowflake``.")
    args: list[ConnectionFieldSpec] = Field(
        default_factory=list, description="Non-secret fields."
    )
    secrets: list[ConnectionFieldSpec] = Field(
        default_factory=list,
        description="Fields stored in secrets.d/*.json (mode 0o600).",
    )
    note: str | None = Field(
        default=None,
        description=(
            "Optional connector-level guidance rendered above the form. "
            "Used to explain non-obvious defaults (e.g. that leaving every "
            "Azure secret blank routes through the host's managed identity)."
        ),
    )


class ConnectionSummary(BaseModel):
    """Lightweight listing entry for the connections index."""

    name: str
    type: str
    path: str
    secret_ref: str | None = None


class ConnectionDetail(BaseModel):
    """One connection with masked secret field placeholders.

    Values in ``args`` are returned verbatim (never sensitive by
    definition). Values in ``secrets`` are always the sentinel string
    ``"***"`` — the web tier never exposes real credential material.
    """

    name: str
    type: str
    path: str
    args: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Every value is ``'***'``; keys reflect the referenced secret file.",
    )
    auth_mode: str | None = Field(
        default=None,
        description=(
            "Connector-computed short label describing the auth path the "
            "instance will follow when :meth:`BaseConnection.test` runs. "
            "Populated only for connectors that expose ``_auth_mode`` "
            "(currently ``azure_blob``); ``None`` otherwise."
        ),
    )


class ConnectionWrite(BaseModel):
    """Payload for creating or updating a connection.

    ``args`` carries the non-secret form values. ``secrets`` may
    include real values (they are written to a mode-0o600 secret file)
    or the sentinel ``"***"`` to keep an existing value.
    """

    type: str = Field(..., description="One of the registered connector types.")
    args: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        None,
        description=(
            "Filename stem of the secret file backing this connection. "
            "Defaults to ``conn_<name>`` when secrets are supplied and no "
            "reference is provided."
        ),
    )


class ConnectionTestReport(BaseModel):
    """Structured result of ``POST /api/v1/connections/{name}/test``."""

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Address (W7 widening)
# ---------------------------------------------------------------------------
#
# The ``AddressWrite`` model above (W4) keeps its ``src_name`` /
# ``src_args`` fields for backwards compatibility. The W7 shape adds
# optional ``connection`` and ``location`` fields; when both new fields
# are present the write path uses them and ignores the legacy pair.
# Adding them as optional avoids a breaking change to the OpenAPI
# schema surface for existing clients.


# ---------------------------------------------------------------------------
# Pipelines / saved job configs (W7, extended by C1)
# ---------------------------------------------------------------------------


#: Supported pipeline kinds. ``etl`` is the primary kind — a
#: full ETL pipeline gated by a :class:`~ovid2.cdm.handle.CdmHandle`
#: (this kind was historically named ``cdm``; see :data:`KIND_ALIASES`
#: and :func:`normalize_kind` for the back-compat mapping).
#: ``analysis`` runs a saved analysis job (read-only). ``dq_checks``
#: runs DQ checks and produces reports. ``copy`` is a one-shot
#: address-to-address move that bypasses the formatter, mapper, and
#: fixer stages — added post-C1 to expose ``ovid2 tools copy`` on the
#: web + MCP surfaces (see the "Copy job kind" ADR delta). A copy
#: pipeline reads every named table from ``input_address`` and writes it
#: to ``output_address`` verbatim; ``cdm`` / ``transformations`` /
#: ``sf_*`` are meaningless and scrubbed by ``_validate_kind``.
#: ``move`` is the driver-native successor to ``copy`` — same
#: semantics but routes I/O through the D3 native drivers instead of
#: the pre-refactor ``BaseAddress`` chain, and adds a
#: ``strict_types`` flag toggling the D6.0 advisory CDM type check
#: between warn and raise. Added post-M1 to expose ``ovid2 tools
#: move`` on the web + MCP surfaces.
#:
#: As of the run-kind-registry refactor these three symbols are *derived*
#: from :mod:`ovid2.run_kinds` — the single, declarative source of truth
#: for run kinds (see that module for the per-kind :class:`KindSpec`).
#: They are kept here as computed aliases so every existing importer
#: (``PIPELINE_KINDS`` / ``PACKAGE_FREE_KINDS`` / ``KIND_ALIASES`` /
#: :func:`normalize_kind`) keeps working unchanged. Add a new kind by
#: adding one ``KindSpec`` in :mod:`ovid2.run_kinds`, not by editing
#: these tuples.
from ovid2.run_kinds import KIND_ALIASES as KIND_ALIASES  # noqa: E402
from ovid2.run_kinds import all_kinds as _all_kinds  # noqa: E402
from ovid2.run_kinds import normalize_kind as normalize_kind  # noqa: E402
from ovid2.run_kinds import package_free_kinds as _package_free_kinds  # noqa: E402

#: Supported pipeline kinds, in display order. Derived from
#: :data:`ovid2.run_kinds.KIND_REGISTRY`.
PIPELINE_KINDS: tuple[str, ...] = _all_kinds()

#: Kinds whose executor never imports an ``ovid2_pipelines.<name>``
#: package (the ``pipeline`` field is only a name/label). Derived from
#: the ``package_required`` flag on each :class:`~ovid2.run_kinds.KindSpec`.
PACKAGE_FREE_KINDS: frozenset[str] = _package_free_kinds()


class PipelineTrigger(BaseModel):
    """Trigger cadence for a pipeline.

    Supported types: ``manual``, ``cron``, ``interval``, and ``watch``.

    - ``manual`` — never fires on its own; only ``run_now`` submits.
    - ``cron`` — evaluated with :mod:`croniter` on each worker tick.
    - ``interval`` — fires every ``interval_minutes`` from last fire.
    - ``watch`` — event-driven: the web-worker polls the pipeline's
      input storage account for a new top-level folder/batch since a
      stored cursor and auto-submits one job per new batch. It is not
      clock-driven, so :func:`pipelines_svc.compute_next_fire` returns
      ``None`` for it. ``watch_min_wait_minutes`` debounces re-checks
      of the same input.
    """

    type: str = Field(
        ..., description="``manual`` | ``cron`` | ``interval`` | ``watch``."
    )
    cron: str | None = Field(None, description="Crontab expression (when type=cron).")
    interval_minutes: int | None = Field(
        None, description="Minutes between fires (when type=interval)."
    )
    watch_min_wait_minutes: int | None = Field(
        None,
        description=(
            "Debounce (minutes) between re-scans of a watched input "
            "storage account (when type=watch). ``None`` scans every tick."
        ),
    )


class PipelineConfigDetail(BaseModel):
    """One pipeline config stored under ``$XDG_CONFIG_HOME/ovid2/pipelines/``."""

    name: str
    path: str
    kind: str = Field(
        "etl",
        description=(
            "Pipeline kind. One of `etl`, `analysis`, `dq_checks`, "
            "`copy`, `move`. Determines which pipeline the worker "
            "invokes on dispatch. `copy` is a one-shot "
            "address-to-address move that bypasses the "
            "formatter/mapper/fixer stages. `move` is the "
            "driver-native successor to `copy` — same semantics, "
            "routes I/O through the D3 native drivers, and honours a "
            "`strict_types` flag on the D6.0 advisory CDM type check. "
            "The deprecated alias `cdm` is accepted and normalised to "
            "`etl`."
        ),
    )
    cdm: str | None = Field(
        None,
        description=(
            "CDM name (e.g. `pcornet_7_0`). Required when `kind=etl`; "
            "ignored otherwise. Must reference a CDM registered in "
            "`ovid2.cdm.registry`."
        ),
    )
    pipeline: str
    input_address: str
    output_address: str
    tables: list[str] = Field(default_factory=lambda: ["all"])
    transformations: list[str] = Field(default_factory=lambda: ["all"])
    packages: list[str] = Field(
        default_factory=list,
        description=(
            "`kind=dq_checks` (and `health`) only. EDC/DC report-cycle "
            "packages (e.g. `20r1`) this pipeline runs — the pipeline's "
            "'type'. Empty means the runner defaults to the latest "
            "available cycle."
        ),
    )
    engine: str = "pyspark"
    workers: int = 25
    sf_database: str | None = None
    sf_account: str | None = None
    sf_schema: str | None = None
    stderr_log: bool = False
    enabled: bool = True
    trigger: PipelineTrigger = Field(default_factory=lambda: PipelineTrigger(type="manual"))
    last_fired_at: str | None = None
    next_fire_at: str | None = None
    last_job_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PipelineConfigSummary(BaseModel):
    """Lightweight listing entry for the pipelines index page."""

    name: str
    kind: str = "etl"
    cdm: str | None = None
    pipeline: str
    input_address: str
    output_address: str
    tables: list[str] = Field(default_factory=list)
    trigger_type: str
    enabled: bool
    last_fired_at: str | None = None
    next_fire_at: str | None = None


class PipelineGroup(BaseModel):
    """One collapsible block on the pipelines page: pipelines of one kind."""

    kind: str
    pipelines: list[PipelineConfigSummary] = Field(default_factory=list)


class PipelineWrite(BaseModel):
    """Payload for creating or updating a pipeline config."""

    kind: str = Field(
        "etl",
        description=(
            "Pipeline kind. One of `etl`, `analysis`, `dq_checks`, "
            "`copy`, `move`. The deprecated alias `cdm` is accepted "
            "and normalised to `etl`."
        ),
    )
    cdm: str | None = Field(
        None,
        description=(
            "CDM name — required when `kind=etl`, ignored otherwise."
        ),
    )
    pipeline: str
    input_address: str
    output_address: str
    tables: list[str] = Field(default_factory=lambda: ["all"])
    transformations: list[str] = Field(default_factory=lambda: ["all"])
    packages: list[str] = Field(
        default_factory=list,
        description=(
            "`kind=dq_checks` (and `health`) only. EDC/DC report-cycle "
            "packages (e.g. `20r1`) this pipeline runs — the pipeline's "
            "'type'. Empty means the runner defaults to the latest "
            "available cycle."
        ),
    )
    engine: str = "pyspark"
    workers: int = Field(25, ge=1, le=200)
    sf_database: str | None = None
    sf_account: str | None = None
    sf_schema: str | None = None
    stderr_log: bool = False
    enabled: bool = True
    trigger: PipelineTrigger = Field(default_factory=lambda: PipelineTrigger(type="manual"))


class PipelineRunRequest(BaseModel):
    """Optional body for ``POST /pipelines/{name}/run``.

    Every field is optional; an empty body runs the pipeline exactly as
    configured. Overrides are applied for this one run only and are never
    persisted back to the config.
    """

    batch_id: str | None = Field(
        None,
        description=(
            "Override the synthesised timestamp batch id for this run."
        ),
    )
    tables: list[str] | None = Field(
        None,
        description=(
            "Override the pipeline's stored tables for this run. Required "
            "in practice for `copy` / `move` pipelines created *open* on "
            "tables — each entry is a logical name (`HASH_TOKEN`) or a "
            "`LOGICAL=SOURCE` split. `null` (the default) keeps the "
            "config's stored tables. `['all']` is rejected for copy / "
            "move by submit-time validation."
        ),
    )
    input_address: str | None = Field(
        None,
        description=(
            "Bind the input side for this run (address config name). "
            "Required when the pipeline keeps its input open."
        ),
    )
    output_address: str | None = Field(
        None,
        description=(
            "Bind the output side for this run (address config name). "
            "Required when the pipeline keeps its output open."
        ),
    )
    draft: bool = Field(
        False,
        description=(
            "Create a *draft* job instead of queueing it: fully specified "
            "and shown on its run page, but only executed when a human "
            "launches it (POST /jobs/{job_id}/launch)."
        ),
    )


# ---------------------------------------------------------------------------
# Global settings (W7)
# ---------------------------------------------------------------------------


class SettingsDefaults(BaseModel):
    """Default engine + workers applied to new submissions."""

    engine: str = "pyspark"
    workers: int = Field(25, ge=1, le=200)


class SettingsWorker(BaseModel):
    """Web-worker knobs."""

    poll_interval_seconds: float = Field(2.0, ge=0.1, le=600.0)
    route_dispatch_enabled: bool = True


class SettingsUi(BaseModel):
    """UI polling knobs (milliseconds) and display preferences."""

    dashboard_poll_ms: int = Field(3000, ge=250, le=60_000)
    run_events_poll_ms: int = Field(2000, ge=250, le=60_000)
    #: IANA timezone name used to render stored UTC timestamps in the
    #: web UI (e.g. ``America/New_York``, ``UTC``, ``Europe/London``).
    #: Timestamps are always *stored* in UTC; this only affects display.
    display_timezone: str = Field("America/New_York", min_length=1, max_length=64)

    @field_validator("display_timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        """Reject unknown IANA timezone names.

        Falls back to raising a ``ValueError`` (surfaced as a 400 /
        ``?error=`` on the settings form) rather than silently accepting
        a name that ``zoneinfo`` can't resolve.
        """
        name = value.strip() or "UTC"
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {name!r}") from exc
        return name



class SettingsNotifications(BaseModel):
    """SMTP notification settings.

    Values are stored on disk in the settings JSON. The optional
    ``smtp_secret_ref`` points at a mode-0o600 secret file that
    supplies the SMTP password separately; it is never returned in
    plaintext by the API.
    """

    smtp_host: str = ""
    smtp_port: int = Field(25, ge=1, le=65_535)
    smtp_from: str = ""
    smtp_to: list[str] = Field(default_factory=list)
    smtp_secret_ref: str | None = None


class SettingsRetention(BaseModel):
    """Retention windows (days) for on-disk logs and DB rows."""

    event_log_days: int = Field(30, ge=1, le=3650)
    web_jobs_days: int = Field(90, ge=1, le=3650)


class SettingsFsPicker(BaseModel):
    """Host filesystem path-picker preferences.

    The picker powers the ``disc.host_path`` widget on the connection
    form. It runs against the web tier's local filesystem (the web
    tier is host-managed via systemd, not containerised) and is
    read-only.

    Attributes:
        enabled: Kill-switch. When False, both ``/api/v1/fs/*``
            endpoints return 404 and the connection form falls back
            to a plain text input.
        roots: Allowlist of absolute path prefixes the picker may
            traverse. Defaults to a single ``/`` root; a paranoid
            deployment can pin it to e.g. ``["/data", "/processing"]``
            so the browsable surface only exposes shared trees.
        show_hidden: When True, dotfiles / dot-directories are
            included in listings by default.
    """

    enabled: bool = True
    roots: list[str] = Field(default_factory=lambda: ["/"])
    show_hidden: bool = False


class SettingsOds(BaseModel):
    """Ovid Data Space (naming layer) preferences.

    Every field is optional and default-empty; deployments that do not
    care about ODS can leave the section unset entirely.

    - ``default_dimension`` — dimension chosen when a caller omits
      ``dim=``. Defaults to ``disc``, matching the historical shape
      of every pipeline deployment shipped before the ODS phase.
    - ``dimension_defaults`` — map ``{dimension: address_name}``
      pointing at the address that should serve a dimension when no
      per-pipeline override applies.
    - ``pipeline_routes`` — nested map
      ``{pipeline: {dimension: address_name}}`` for pipelines that
      need a specific address for a specific dimension (e.g. one
      pipeline uses a different Snowflake database).
    - ``pipeline_dimensions`` — map ``{pipeline: dimension}`` that
      picks a pipeline's *default* dimension when the caller does
      not pass ``dim=``. Rarely needed; useful when a pipeline's
      canonical source lives outside of disc.
    """

    default_dimension: str = "disc"
    dimension_defaults: dict[str, str] = Field(default_factory=dict)
    pipeline_routes: dict[str, dict[str, str]] = Field(default_factory=dict)
    pipeline_dimensions: dict[str, str] = Field(default_factory=dict)


class SettingsAdmin(BaseModel):
    """Admin storage-account (Ovid2's own bookkeeping) preferences.

    The admin storage account holds Ovid2's *own* operational state —
    scheduler / job / audit / watch-cursor / run bookkeeping — as opposed
    to the pipeline/pipeline data the pipelines move around. It is modelled
    as a first-class Ovid2 storage account (a *connection* by name).

    - ``storage_account`` — the name of a registered connection that
      backs the admin state. ``None`` (the default) means "use the local
      ``scheduler.db`` at its XDG-resolved path, exactly as today"; no
      behaviour changes until an operator points this at a connection.
    """

    storage_account: str | None = Field(
        None,
        description=(
            "Name of a registered connection that backs Ovid2's own "
            "scheduler/job/audit/watch-cursor/run state. `None` means "
            "use the local scheduler.db as today."
        ),
    )

    @field_validator("storage_account")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        """Treat a blank string as unset so `""` and `None` are equivalent."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None
class AuthProviderConfig(BaseModel):
    """Configuration for an authentication provider (OIDC)."""

    display_name: str = Field(..., description="Human-readable name for the provider")
    issuer: str = Field(..., description="OIDC discovery issuer URL")
    client_id: str = Field(..., description="OAuth 2.0 client ID")
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"], description="OIDC scopes to request")
    enabled: bool = Field(default=True, description="Whether this provider is enabled")


class SettingsAuth(BaseModel):
    """Authentication settings."""
    enabled: bool = Field(default=False, description="Whether authentication is enabled")
    providers: dict[str, AuthProviderConfig] = Field(default_factory=dict, description="Authentication providers configuration")
    bootstrap_admin_emails: list[str] = Field(default_factory=list, description="Email addresses of bootstrap administrators")
    absolute_ttl_minutes: int = Field(default=480, description="Absolute session TTL in minutes (8 hours)")
    idle_ttl_minutes: int = Field(default=45, description="Idle session TTL in minutes")
    silent_recheck: bool = Field(default=True, description="Whether to silently recheck token validity")
    cookie_name: str = Field(default="ovid2_session", description="Name of the session cookie")
    trust_forwarded_proto: bool = Field(default=True, description="Whether to trust X-Forwarded-Proto header")
    service_principal_role: str = Field(
        default="pipeline_operator",
        description=(
            "Fallback role granted to a service-principal bearer token "
            "(MCP tier) when its stored role is missing or invalid. Must "
            "not be system_admin."
        ),
    )

    @field_validator("bootstrap_admin_emails")
    @classmethod
    def _normalize_emails(cls, value: list[str]) -> list[str]:
        """Lowercase, strip whitespace, and remove empty emails."""
        if not value:
            return []
        normalized = []
        for email in value:
            if isinstance(email, str):
                email = email.strip().lower()
                if email:  # Only add non-empty strings
                    normalized.append(email)
        return normalized


class SettingsLlm(BaseModel):
    """Dual-system LLM routing (L1).

    ``general`` and ``system1`` name LLM connections under
    ``$XDG_CONFIG_HOME/ovid2/llms/``. System 1 (JEV) is consulted first;
    when it abstains, errors, or its confidence is below
    ``confidence_threshold`` the turn falls back to ``general``.
    """

    enabled: bool = Field(
        default=False,
        description="Kill-switch for the right-panel assistant. When False the "
                    "chat endpoint returns 404.",
    )
    general: str | None = Field(
        default=None, description="General LLM connection name."
    )
    system1: str | None = Field(
        default=None, description="System 1 decision LLM connection name (e.g. JEV)."
    )
    confidence_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="Minimum System 1 confidence to accept its decision.",
    )
    fallback_to_general: bool = Field(
        default=True,
        description="Route to the general LLM when System 1 does not accept.",
    )
    max_concurrent_chats: int = Field(
        default=4, ge=1, le=64,
        description="Upper bound on simultaneous chat executor subprocesses.",
    )
    chat_timeout_seconds: int = Field(
        default=180, ge=10, le=1800,
        description="Wall-clock ceiling for one chat turn.",
    )
    tools_enabled: bool = Field(
        default=False,
        description="Let the general LLM call Ovid2 API tools (agent_tools "
                    "catalogue). GET tools run with a per-turn read-only token "
                    "scoped to the chatting user (capped at pipeline_operator); "
                    "write tools are shown as proposals the user must run.",
    )
    max_tool_iterations: int = Field(
        default=6, ge=1, le=20,
        description="Upper bound on model round-trips per turn when tools are on.",
    )
    tool_result_max_chars: int = Field(
        default=8000, ge=256, le=200000,
        description="Cap on one tool result fed back to the model.",
    )


class GlobalSettings(BaseModel):
    """Top-level runtime preferences persisted in ``settings.json``."""

    defaults: SettingsDefaults = Field(default_factory=SettingsDefaults)
    worker: SettingsWorker = Field(default_factory=SettingsWorker)
    ui: SettingsUi = Field(default_factory=SettingsUi)
    notifications: SettingsNotifications = Field(default_factory=SettingsNotifications)
    retention: SettingsRetention = Field(default_factory=SettingsRetention)
    ods: SettingsOds = Field(default_factory=SettingsOds)
    fs_picker: SettingsFsPicker = Field(default_factory=SettingsFsPicker)
    admin: SettingsAdmin = Field(default_factory=SettingsAdmin)
    auth: SettingsAuth = Field(default_factory=SettingsAuth)
    llm: SettingsLlm = Field(default_factory=SettingsLlm)


# ---------------------------------------------------------------------------
# Sessions (auth)
# ---------------------------------------------------------------------------


class SessionRecord(BaseModel):
    """Web session record for authentication tracking."""

    session_id: str
    user_id: str
    created_at: str
    last_seen_at: str
    expires_at: str
    absolute_deadline: str
    idp_sid: str | None
    revoked: bool


# ---------------------------------------------------------------------------
# Engines (E1)
# ---------------------------------------------------------------------------


class EngineFieldSpec(BaseModel):
    """Description of one form field for an engine type.

    Consumed by the web UI to render the dynamic form (only fields
    relevant to the selected engine show up) and by MCP tools to
    validate submissions client-side. Shape matches
    :class:`ConnectionFieldSpec` on purpose so the UI's form-partial
    can be reused verbatim.
    """

    name: str
    required: bool = False
    default: Any | None = None
    help: str = ""


class EngineTypeSpec(BaseModel):
    """Field spec catalogue entry for one engine type."""

    type: str = Field(..., description="Machine name, e.g. ``pyspark``.")
    args: list[EngineFieldSpec] = Field(
        default_factory=list, description="Non-secret fields."
    )
    secrets: list[EngineFieldSpec] = Field(
        default_factory=list,
        description="Fields stored in secrets.d/*.json (mode 0o600).",
    )


class EngineSummary(BaseModel):
    """Lightweight listing entry for the engines index."""

    name: str
    type: str
    path: str
    secret_ref: str | None = None


class EngineDetail(BaseModel):
    """One engine with masked secret field placeholders.

    Values in ``args`` are returned verbatim (compute knobs are never
    secret). Values in ``secrets`` are always the sentinel string
    ``"***"`` — the web tier never exposes real credential material.
    """

    name: str
    type: str
    path: str
    args: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Every value is ``'***'``; keys reflect the referenced secret file.",
    )


class EngineWrite(BaseModel):
    """Payload for creating or updating an engine config.

    ``args`` carries the non-secret compute knobs (spark.executor.memory,
    workspace_url, snowflake account, ...). ``secrets`` may include real
    values (written to a mode-0o600 secret file) or the sentinel
    ``"***"`` to keep the existing value.
    """

    type: str = Field(..., description="One of the registered engine types.")
    args: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        None,
        description=(
            "Filename stem of the secret file backing this engine. "
            "Defaults to ``engine_<name>`` when secrets are supplied and no "
            "reference is provided."
        ),
    )


class EngineTestReport(BaseModel):
    """Structured result of ``POST /api/v1/engines/{name}/test``."""

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# LLM connections (L1)
# ---------------------------------------------------------------------------


class LlmFieldSpec(BaseModel):
    """One form field for an LLM provider (same shape as connection specs)."""

    name: str
    required: bool = False
    default: Any | None = None
    help: str = ""
    multiline: bool = False


class LlmProviderSpec(BaseModel):
    """Field-spec catalogue entry for one LLM provider."""

    provider: str = Field(..., description="Provider name, e.g. ``openai_compatible``.")
    roles: list[str] = Field(default_factory=list, description="Roles this provider can fill.")
    args: list[LlmFieldSpec] = Field(default_factory=list)
    secrets: list[LlmFieldSpec] = Field(default_factory=list)
    note: str = ""


class LlmSummary(BaseModel):
    """Listing entry for the LLMs index."""

    name: str
    provider: str
    role: str
    model: str = ""
    base_url: str = ""
    path: str
    secret_ref: str | None = None


class LlmDetail(LlmSummary):
    """One LLM connection with masked secret placeholders."""

    args: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(
        default_factory=dict, description="Every value is ``'***'``."
    )


class LlmWrite(BaseModel):
    """Payload for creating / updating an LLM connection."""

    provider: str = Field(..., description="One of the registered LLM providers.")
    role: str = Field("general", description="``general`` or ``system1``.")
    args: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(
        default_factory=dict,
        description="Real values (written 0o600) or ``'***'`` to keep the existing value.",
    )
    secret_ref: str | None = Field(
        None, description="Secret file stem; defaults to ``llm_<name>``."
    )


class LlmTestReport(BaseModel):
    """Structured result of ``POST /api/v1/llms/{name}/test``."""

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class LlmChatMessage(BaseModel):
    """One conversation turn sent by the chat panel."""

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=32000)


class LlmUiContext(BaseModel):
    """The browser's current look, sent so UI-action tools can reason about it.

    Purely cosmetic, user-owned browser state (localStorage). Bounded and
    pattern-checked here, re-sanitised by the executor, and never audited.
    """

    theme: str | None = Field(None, max_length=40, pattern=r"^[a-z0-9-]{1,40}$")
    colors: dict[str, str] = Field(default_factory=dict, max_length=8)
    appearance: dict[str, str] = Field(default_factory=dict, max_length=40)
    saved_themes: list[str] = Field(default_factory=list, max_length=30)


class LlmChatRequest(BaseModel):
    """Body of ``POST /api/v1/llm/chat/stream``.

    The browser owns the conversation history (nothing is stored
    server-side); every turn re-sends the trailing window.
    """

    messages: list[LlmChatMessage] = Field(..., min_length=1, max_length=40)
    ui_context: LlmUiContext | None = None


class LlmStatus(BaseModel):
    """Assistant availability, consumed by the right-panel widget."""

    enabled: bool
    general: str | None = None
    general_model: str | None = None
    system1: str | None = None
    system1_model: str | None = None
    confidence_threshold: float = 0.7
    fallback_to_general: bool = True
    tools_enabled: bool = False


class LlmToolInfo(BaseModel):
    """One agent tool the assistant may call (``GET /api/v1/llm/tools``)."""

    name: str
    description: str
    method: str
    path: str
    capability: str
    mutating: bool
    opens_draft: bool = Field(
        False, description="Draft-job tool executed by the browser without a click."
    )
    ui_action: bool = Field(
        False, description="Browser-only cosmetic action (no HTTP call); applied with Undo."
    )
    kind: str = Field("read", description="read / proposal / draft / ui_action.")
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ovid Data Space (ODS)
# ---------------------------------------------------------------------------


class OdsDimensionSummary(BaseModel):
    """One dimension in the ODS namespace.

    Dimensions are connection-type families — every registered
    connector (``disc``, ``snowflake``, ``s3``, ``azure_blob``, …)
    contributes exactly one.
    """

    name: str = Field(..., description="Dimension name (matches a connection type).")
    address_count: int = Field(0, ge=0, description="How many address configs fall under this dimension.")
    default_address: str | None = Field(
        None,
        description="The address configured as this dimension's default (settings.ods.dimension_defaults).",
    )


class OdsDimensionCatalog(BaseModel):
    """Response for ``GET /api/v1/dataspace/dimensions``."""

    default_dimension: str = Field(..., description="Global default from settings.ods.default_dimension.")
    dimensions: list[OdsDimensionSummary] = Field(default_factory=list)


class OdsPipelineSummary(BaseModel):
    """One row in the root ODS listing."""

    name: str
    known_dimensions: list[str] = Field(
        default_factory=list,
        description="Dimensions that have at least one address for this pipeline.",
    )


class OdsPipelineListing(BaseModel):
    """Response for ``GET /api/v1/dataspace``."""

    pipelines: list[OdsPipelineSummary] = Field(default_factory=list)


class OdsBatchSummary(BaseModel):
    """One batch under a pipeline."""

    batch_id: str
    dimensions: list[str] = Field(default_factory=list)


class OdsBatchListing(BaseModel):
    """Response for ``GET /api/v1/dataspace/{pipeline}``."""

    pipeline: str
    batches: list[OdsBatchSummary] = Field(default_factory=list)


class OdsTableSummary(BaseModel):
    """One table under a batch."""

    name: str
    dimensions: list[str] = Field(default_factory=list)


class OdsTableListing(BaseModel):
    """Response for ``GET /api/v1/dataspace/{pipeline}/{batch}``."""

    pipeline: str
    batch_id: str
    tables: list[OdsTableSummary] = Field(default_factory=list)


class CdmSummary(BaseModel):
    """One CDM registered in :mod:`ovid2.cdm.registry` (phase C1).

    Returned by ``GET /api/v1/cdms`` and consumed by the pipelines UI to
    populate the ``cdm`` <select>. ``has_yaml`` distinguishes the
    YAML-authored CDMs under ``src/ovid2/resources/cdms/`` from the
    pipeline-family CDMs (nursing / veritas / tumor) that are still
    declared in Python.
    """

    name: str
    version: str = ""
    table_count: int
    has_yaml: bool = False


class CdmSharedView(BaseModel):
    """One :class:`~ovid2.cdm.handle.SharedViewSpec` flattened for the API."""

    view_name: str
    source_path: str
    delimiter: str = "\t"
    header: bool = True
    required: bool = False


class CdmIdColumn(BaseModel):
    """One :class:`~ovid2.cdm.handle.IdColumn` entry."""

    role: str
    column: str
    width: int = 64
    reference_table: str | None = None


class CdmTableSummary(BaseModel):
    """One table in a CDM, with its composite dedup key."""

    name: str
    composite_key: list[str] = Field(default_factory=list)
    is_reference: bool = False


class CdmValuesetSummary(BaseModel):
    """One bounded valueset registered on the CDM."""

    table: str
    column: str
    code_count: int


class CdmDetail(BaseModel):
    """Full detail for one CDM handle.

    Assembled by :mod:`ovid2.web.services.cdms_svc` from
    :class:`ovid2.cdm.handle.CdmHandle`. The web tier reads only
    metadata — no pyspark, no schema file I/O beyond what the loader
    already performed at import time.
    """

    name: str
    version: str = ""
    has_yaml: bool = False
    yaml_dir: str | None = None
    tables: list[CdmTableSummary] = Field(default_factory=list)
    reference_tables: list[str] = Field(default_factory=list)
    id_columns: list[CdmIdColumn] = Field(default_factory=list)
    flavour_of_null: list[str | None] = Field(default_factory=list)
    shared_views: list[CdmSharedView] = Field(default_factory=list)
    valuesets: list[CdmValuesetSummary] = Field(default_factory=list)


class OdsResolveResult(BaseModel):
    """Response for ``GET /api/v1/dataspace/resolve``.

    Contains the naming information but never the credentials or the
    concrete address object — the web tier deliberately keeps the
    pyspark import surface at zero.
    """

    path: str
    pipeline: str | None = None
    batch_id: str | None = None
    table: str | None = None
    dimension: str
    address_name: str
    tier: str = Field(
        ...,
        description=(
            "Which resolution rule fired: 'explicit', 'pipeline_route', "
            "'dimension_default', or 'sole_candidate'."
        ),
    )
    candidates: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Filesystem path picker (disc-mount / path-picker phase)
# ---------------------------------------------------------------------------


class FsEntry(BaseModel):
    """One directory entry in a picker listing.

    Only the fields the picker actually renders are exposed. The web
    tier deliberately does not surface owner / permission bits so a
    misconfigured allowlist cannot become a filesystem-recon vector.
    """

    name: str
    type: str = Field(
        ...,
        description="'dir' | 'file' | 'symlink' | 'other'.",
    )
    is_symlink: bool = False


class FsListing(BaseModel):
    """Response for ``GET /api/v1/fs/list``.

    ``entries`` is directory-first, then alphabetical within each
    kind. When ``show_hidden`` was False on the request, dot-entries
    are omitted.
    """

    path: str
    parent: str | None = None
    entries: list[FsEntry] = Field(default_factory=list)
    show_hidden: bool = False
    allowed: bool = True


class FsRoot(BaseModel):
    """One curated starting point offered by the picker."""

    label: str
    path: str
    source: str = Field(
        ...,
        description=(
            "Origin of the root: 'home', 'allowlist', 'settings', "
            "'connection', or 'system'."
        ),
    )


class FsRoots(BaseModel):
    """Response for ``GET /api/v1/fs/roots``."""

    roots: list[FsRoot] = Field(default_factory=list)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Users (W6)
# ---------------------------------------------------------------------------


class UserRecord(BaseModel):
    """Complete user record from the web_users table."""

    user_id: str
    issuer: str
    subject: str
    email: str | None = None
    name: str | None = None
    role: str
    status: str
    created_at: str
    updated_at: str
    last_login_at: str | None = None


class UserSummary(BaseModel):
    """Lightweight user record for listing endpoints."""

    user_id: str
    email: str | None = None
    name: str | None = None
    role: str
    status: str
    last_login_at: str | None = None


class UserDetail(UserRecord):
    """Full user record returned by the detail and mutation endpoints.

    Aliases the :class:`UserRecord` shape (the complete ``web_users`` row):
    it carries no session ids, break-glass hashes, or issuer/subject
    internals beyond the fields ``UserRecord`` already exposes.
    """


class UserActivateRequest(BaseModel):
    """Request body for activating a pending/disabled user with a role.

    Attributes:
        role: The role string to assign on activation (e.g. ``viewer``,
            ``pipeline_operator``, ``system_admin``). ``pending`` is
            rejected by the route with HTTP 400.
    """

    role: str


class UserRoleRequest(BaseModel):
    """Request body for changing an existing user's role.

    Attributes:
        role: The new role string to assign (e.g. ``viewer``,
            ``pipeline_operator``, ``system_admin``). ``pending`` is
            rejected by the route with HTTP 400.
    """

    role: str
