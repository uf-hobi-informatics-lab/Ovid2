"""Pipeline service — persisted, named ETL job configs (W7).

A **Pipeline** is a small JSON file that names one recurring ETL
invocation. When the pipeline's trigger fires (manually via the *Run
now* button, on a cron schedule, or on a fixed interval) the service
converts the pipeline into a :class:`JobSubmitRequest` and hands it to
:mod:`ovid2.web.services.jobs_svc`, so pipelines reuse every guarantee
the existing job worker offers (crash recovery, cancellation, audit).

Storage:

- Definitions: ``$XDG_CONFIG_HOME/ovid2/pipelines/<name>.json`` (mode 0o644).
- Runtime state: ``web_pipelines_state`` table on ``scheduler.db``. The
  table is created lazily on first write.

Trigger types:

- ``manual`` — never fires on its own; only ``run_now`` submits a job.
- ``cron`` — evaluated with :mod:`croniter` on each worker tick.
- ``interval`` — fires every ``interval_minutes`` from ``last_fired_at``.
- ``watch`` — event-driven new-batch detection on the pipeline's input
  storage account. Not clock-driven; :func:`compute_next_fire` returns
  ``None``. Validated only when the input address resolves to a
  connection whose driver can list directories (see
  :func:`_validate_trigger`). The web-worker's watch dispatch loop
  (:mod:`ovid2.web.services.watch_svc`) polls it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ovid2.config import addresses as _addresses
from ovid2.run_kinds import get_kind
from ovid2.web import db as web_db
from ovid2.web.models import (
    JobSubmitRequest,
    JobSubmitResponse,
    PACKAGE_FREE_KINDS,
    PIPELINE_KINDS,
    PipelineConfigDetail,
    PipelineConfigSummary,
    PipelineGroup,
    PipelineTrigger,
    PipelineWrite,
    WriteResult,
    normalize_kind,
)
from ovid2.web.services import jobs_svc, pipelines_meta_svc

logger = logging.getLogger(__name__)

__all__ = [
    "PipelineError",
    "compute_next_fire",
    "delete_pipeline",
    "due_pipelines",
    "get_pipeline",
    "list_pipelines_grouped",
    "list_pipelines",
    "record_dispatch",
    "run_now",
    "set_enabled",
    "write_pipeline",
]

_TRIGGER_TYPES: frozenset[str] = frozenset({"manual", "cron", "interval", "watch"})
_SAMPLE_SUFFIX: str = "_sample"

_PIPELINES_STATE_DDL = """
CREATE TABLE IF NOT EXISTS web_pipelines_state (
    name           TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_fired_at  TEXT,
    next_fire_at   TEXT,
    last_job_id    TEXT
)
"""


class PipelineError(ValueError):
    """Raised when a pipeline payload is invalid or targets a bad name."""


# ---------------------------------------------------------------------------
# Path + name helpers
# ---------------------------------------------------------------------------


def _search_paths() -> list[Path]:
    """Return the ordered list of directories to search for pipeline JSONs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_PIPELINES_DIR")
    if override:
        paths.append(Path(override))
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg_cfg) / "ovid2" / "pipelines")
    paths.append(Path.cwd() / "pipelines")
    paths.append(Path("/etc/ovid2/pipelines"))
    return paths


def _writable_dir() -> Path:
    """Return the user-scoped write target for pipeline JSONs."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    directory = Path(xdg_cfg) / "ovid2" / "pipelines"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _writable_path(name: str) -> Path:
    """Return the write target for a pipeline JSON."""
    return _writable_dir() / f"{name}.json"


def _validate_name(name: str) -> None:
    """Reject filename stems that would escape the pipelines directory."""
    if not name:
        raise PipelineError("Pipeline name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise PipelineError(
            f"Invalid pipeline name '{name}'. Use letters, digits, '-', '_', '.' only."
        )
    if name.startswith(".") or name.endswith(_SAMPLE_SUFFIX):
        raise PipelineError(f"Pipeline name '{name}' is reserved.")


def _locate(name: str) -> Path | None:
    """Return the on-disk path of a pipeline JSON, or ``None`` when absent."""
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def _list_names() -> list[str]:
    """Return every pipeline filename stem found on the search path."""
    seen: set[str] = set()
    for directory in _search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".json" and not entry.stem.endswith(_SAMPLE_SUFFIX):
                seen.add(entry.stem)
    return sorted(seen)


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _now() -> datetime:
    """Return the current UTC time (module-local for monkeypatching)."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Trigger validation
# ---------------------------------------------------------------------------


def _validate_trigger(
    trigger: PipelineTrigger, *, input_address: str | None = None
) -> None:
    """Reject unsupported trigger shapes.

    ``watch`` triggers require ``input_address`` to resolve to a W7
    connection-shape address whose driver can list directories (so the
    watch loop can enumerate new batch folders). A legacy-shape address
    (no connection) or an address whose driver has no ``list_dir``
    capability is rejected.
    """
    if trigger.type not in _TRIGGER_TYPES:
        raise PipelineError(
            f"Unsupported trigger type '{trigger.type}'. "
            f"Supported: {sorted(_TRIGGER_TYPES)}."
        )
    if trigger.type == "cron":
        if not trigger.cron:
            raise PipelineError("cron triggers require a 'cron' expression.")
        try:
            from croniter import croniter  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — croniter is pinned
            raise PipelineError(
                "croniter is not installed; cannot validate cron trigger."
            ) from exc
        if not croniter.is_valid(trigger.cron):
            raise PipelineError(f"Invalid cron expression: '{trigger.cron}'.")
    if trigger.type == "interval":
        if trigger.interval_minutes is None or trigger.interval_minutes < 1:
            raise PipelineError(
                "interval triggers require interval_minutes >= 1."
            )
    if trigger.type == "watch":
        wait = trigger.watch_min_wait_minutes
        if wait is not None and wait < 0:
            raise PipelineError("watch_min_wait_minutes must be >= 0.")
        _assert_watchable_input(input_address)


def _assert_watchable_input(input_address: str | None) -> None:
    """Raise unless ``input_address`` can back a ``watch`` trigger.

    The input must be a W7 connection-shape address (so the watch loop
    can browse its storage account) and that storage account's driver
    must support directory listing.
    """
    from ovid2.web.services import addresses_svc, storage_browse_svc

    if not (input_address or "").strip():
        raise PipelineError(
            "watch triggers need a bound input storage account, but this "
            "pipeline keeps its input open (chosen at run time). Bind an "
            "input, or use a manual trigger and run it from the Submit form."
        )
    detail = addresses_svc.get_address(input_address or "")
    if detail is None:
        raise PipelineError(
            f"watch triggers require a resolvable input_address; "
            f"'{input_address}' was not found."
        )
    if not detail.connection:
        raise PipelineError(
            f"watch triggers require a connection-shape input_address; "
            f"'{input_address}' is a legacy address with no storage account."
        )
    response = storage_browse_svc.browse_storage(detail.connection)
    if not response.supported:
        raise PipelineError(
            f"watch triggers require an input storage account whose driver "
            f"can list directories; connection '{detail.connection}' cannot."
        )


def compute_next_fire(
    trigger: PipelineTrigger,
    *,
    now: datetime | None = None,
    last_fired_at: datetime | None = None,
) -> datetime | None:
    """Return the next scheduled fire time for a trigger.

    Returns ``None`` for ``manual`` and ``watch`` triggers (``watch`` is
    event-driven, not clock-driven). For ``cron`` uses
    ``croniter.get_next``; for ``interval`` returns
    ``max(now, last_fired_at + interval)``.
    """
    reference = now or _now()
    if trigger.type == "manual":
        return None
    if trigger.type == "cron":
        from croniter import croniter  # type: ignore[import-not-found]
        base = last_fired_at or reference
        itr = croniter(str(trigger.cron), base)
        next_dt: datetime = itr.get_next(datetime)
        if next_dt <= reference:
            # last_fired_at is in the past; catch up to the next slot
            # strictly after 'now' to avoid a burst of back-fires.
            itr = croniter(str(trigger.cron), reference)
            next_dt = itr.get_next(datetime)
        return next_dt.astimezone(timezone.utc)
    if trigger.type == "interval":
        assert trigger.interval_minutes is not None
        step = timedelta(minutes=int(trigger.interval_minutes))
        if last_fired_at is None:
            return reference + step
        candidate = last_fired_at + step
        return max(candidate, reference)
    return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _ensure_state_schema() -> None:
    """Create the ``web_pipelines_state`` table if it does not exist yet."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(_PIPELINES_STATE_DDL)


def _read_state(name: str) -> dict[str, Any] | None:
    """Return one row of ``web_pipelines_state`` as a dict, or ``None``."""
    _ensure_state_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_pipelines_state WHERE name = ?", (name,)
        ).fetchone()
    return dict(row) if row is not None else None


def _write_state(
    name: str,
    *,
    enabled: bool | None = None,
    last_fired_at: str | None = None,
    next_fire_at: str | None = None,
    last_job_id: str | None = None,
) -> None:
    """Upsert one row of ``web_pipelines_state``."""
    _ensure_state_schema()
    existing = _read_state(name)
    if existing is None:
        row_enabled = 1 if enabled is None else (1 if enabled else 0)
        with web_db.connect() as conn:
            conn.execute(
                "INSERT INTO web_pipelines_state "
                "(name, enabled, last_fired_at, next_fire_at, last_job_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, row_enabled, last_fired_at, next_fire_at, last_job_id),
            )
        return
    new_enabled = existing["enabled"] if enabled is None else (1 if enabled else 0)
    new_last = existing["last_fired_at"] if last_fired_at is None else last_fired_at
    new_next = existing["next_fire_at"] if next_fire_at is None else next_fire_at
    new_job = existing["last_job_id"] if last_job_id is None else last_job_id
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_pipelines_state SET enabled = ?, last_fired_at = ?, "
            "next_fire_at = ?, last_job_id = ? WHERE name = ?",
            (new_enabled, new_last, new_next, new_job, name),
        )


def _read_raw(path: Path) -> dict[str, Any]:
    """Return the raw on-disk contents of a pipeline file."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise PipelineError(f"Pipeline file {path} is not a JSON object.")
    return data


# ---------------------------------------------------------------------------
# Model conversion
# ---------------------------------------------------------------------------


def _row_to_detail(name: str, path: Path, doc: dict[str, Any]) -> PipelineConfigDetail:
    """Convert an on-disk document + state row into a :class:`PipelineConfigDetail`."""
    state = _read_state(name) or {}
    trigger_payload: dict[str, Any] = dict(doc.get("trigger") or {"type": "manual"})
    trigger = PipelineTrigger(
        type=str(trigger_payload.get("type", "manual")),
        cron=trigger_payload.get("cron"),
        interval_minutes=trigger_payload.get("interval_minutes"),
        watch_min_wait_minutes=trigger_payload.get("watch_min_wait_minutes"),
    )
    kind = normalize_kind(doc.get("kind"))
    spec = get_kind(kind)
    cdm = doc.get("cdm") or None
    # Rehydration defaults are registry-driven. Kinds that bypass the
    # formatter/mapper/fixer chain (``not uses_transformations`` — copy /
    # move / health / dq_checks) persist an empty ``transformations``
    # list; the ``["all"]`` fallback used for etl-shape kinds must NOT
    # re-hydrate it. An unknown kind (no spec) falls back to the
    # etl-shape default for backwards compatibility.
    uses_transformations = spec.uses_transformations if spec else True
    raw_transformations = doc.get("transformations")
    if uses_transformations:
        transformations = list(raw_transformations or ["all"])
    else:
        transformations = list(raw_transformations or [])
    # ``explicit``-tables kinds (copy / move) have no ``all`` sentinel,
    # and an empty stored list is a deliberate "open" choice (tables
    # picked on /submit). Preserve it verbatim instead of re-hydrating
    # to ``["all"]``. Every other kind (etl / analysis / health /
    # dq_checks) keeps the historical ``["all"]`` fallback — health-shape
    # kinds ignore ``tables`` at argv time, so the value is cosmetic.
    tables_mode = spec.tables_mode if spec else "all"
    raw_tables = doc.get("tables")
    tables = (
        list(raw_tables or [])
        if tables_mode == "explicit"
        else list(raw_tables or ["all"])
    )
    return PipelineConfigDetail(
        name=name,
        path=str(path),
        kind=kind,
        cdm=(str(cdm) if cdm else None),
        pipeline=str(doc.get("pipeline", "")),
        input_address=str(doc.get("input_address", "")),
        output_address=str(doc.get("output_address", "")),
        tables=tables,
        transformations=transformations,
        packages=list(doc.get("packages") or []),
        engine=str(doc.get("engine", "pyspark")),
        workers=int(doc.get("workers", 25)),
        sf_database=doc.get("sf_database") or None,
        sf_account=doc.get("sf_account") or None,
        sf_schema=doc.get("sf_schema") or None,
        stderr_log=bool(doc.get("stderr_log", False)),
        enabled=bool(state.get("enabled", doc.get("enabled", True))),
        trigger=trigger,
        last_fired_at=(str(state["last_fired_at"])
                       if state.get("last_fired_at") else None),
        next_fire_at=(str(state["next_fire_at"])
                      if state.get("next_fire_at") else None),
        last_job_id=(str(state["last_job_id"])
                     if state.get("last_job_id") else None),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def list_pipelines() -> list[PipelineConfigSummary]:
    """Return every pipeline on the search path, with basic runtime state."""
    summaries: list[PipelineConfigSummary] = []
    for name in _list_names():
        path = _locate(name)
        if path is None:
            continue
        try:
            doc = _read_raw(path)
        except (OSError, json.JSONDecodeError, PipelineError):
            continue
        state = _read_state(name) or {}
        trigger = doc.get("trigger") or {"type": "manual"}
        kind = normalize_kind(doc.get("kind"))
        # ``explicit``-tables kinds (copy / move) have no ``all`` sentinel;
        # an empty stored list is a deliberate "open" choice (tables picked
        # at run time). Preserve it verbatim so the Run modal can prefill
        # the tables field. Every other kind keeps the ``["all"]`` default.
        spec = get_kind(kind)
        tables_mode = spec.tables_mode if spec else "all"
        raw_tables = doc.get("tables")
        tables = (
            list(raw_tables or [])
            if tables_mode == "explicit"
            else list(raw_tables or ["all"])
        )
        summaries.append(
            PipelineConfigSummary(
                name=name,
                kind=kind,
                cdm=(str(doc.get("cdm")) if doc.get("cdm") else None),
                pipeline=str(doc.get("pipeline", "")),
                input_address=str(doc.get("input_address", "")),
                output_address=str(doc.get("output_address", "")),
                tables=tables,
                trigger_type=str(trigger.get("type", "manual")),
                enabled=bool(state.get("enabled", doc.get("enabled", True))),
                last_fired_at=(str(state["last_fired_at"])
                               if state.get("last_fired_at") else None),
                next_fire_at=(str(state["next_fire_at"])
                              if state.get("next_fire_at") else None),
            )
        )
    return summaries


def list_pipelines_grouped() -> list[PipelineGroup]:
    """Return every pipeline grouped by kind (type), for the list page.

    Groups are ordered by :data:`ovid2.web.models.PIPELINE_KINDS`; within a
    group, pipelines keep the ``list_pipelines`` (name-sorted) order. Empty
    groups are omitted. Each :class:`PipelineConfigSummary` already carries the
    filterable attributes the list page needs (name, kind, cdm,
    trigger_type, enabled, input/output).
    """
    summaries = list_pipelines()
    by_kind: dict[str, list[PipelineConfigSummary]] = {}
    for summary in summaries:
        by_kind.setdefault(summary.kind, []).append(summary)
    groups: list[PipelineGroup] = []
    for kind in PIPELINE_KINDS:
        rows = by_kind.pop(kind, [])
        if rows:
            groups.append(PipelineGroup(kind=kind, pipelines=rows))
    for kind in sorted(by_kind):
        groups.append(PipelineGroup(kind=kind, pipelines=by_kind[kind]))
    return groups


def get_pipeline(name: str) -> PipelineConfigDetail | None:
    """Return one pipeline's detail (config + runtime state) or ``None``."""
    path = _locate(name)
    if path is None:
        return None
    try:
        doc = _read_raw(path)
    except (OSError, json.JSONDecodeError, PipelineError):
        return None
    return _row_to_detail(name, path, doc)


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def _validate_write(payload: PipelineWrite) -> None:
    """Cheap pre-flight validation shared by create + update."""
    if not payload.pipeline.strip():
        raise PipelineError("pipeline is required.")
    # Package-free kinds (health / copy / move) never import an
    # ``ovid2_pipelines.<name>`` package — ``pipeline`` is only a
    # name/label for address path scoping — so we must not require an
    # installed package for them. Package-consuming kinds (etl, whose
    # package is scaffolded before this runs, plus analysis / dq_checks
    # which resolve per-table formatters from the package) keep the gate.
    if (
        normalize_kind(payload.kind) not in PACKAGE_FREE_KINDS
        and payload.pipeline not in set(pipelines_meta_svc.discover_pipelines())
    ):
        raise PipelineError(
            f"Pipeline '{payload.pipeline}' is not installed under ovid2_pipelines."
        )
    known_addresses = set(_addresses.list_available_addresses())
    if known_addresses:
        for label, value in (
            ("input_address", payload.input_address),
            ("output_address", payload.output_address),
        ):
            # An empty value is the "open" sentinel — the side is bound at
            # run time on the Submit form (which requires it there), so it
            # is not expected on the config search path.
            if value and value not in known_addresses:
                raise PipelineError(
                    f"{label} '{value}' is not on the config search path."
                )
    _validate_kind(payload)
    _validate_trigger(payload.trigger, input_address=payload.input_address)


def _validate_kind(payload: PipelineWrite) -> None:
    """Enforce pipeline-kind semantics, driven by the run-kind registry.

    Every per-kind decision below reads a flag on the kind's
    :class:`~ovid2.run_kinds.KindSpec` (see :mod:`ovid2.run_kinds`)
    rather than hardcoding ``if kind == ...``:

    - ``kind`` must be a registered kind (the deprecated alias ``cdm``
      is accepted and normalised to ``etl``, then written back onto
      ``payload.kind``).
    - ``spec.tables_mode == 'explicit'`` (copy / move): the literal
      ``['all']`` is rejected (``ovid2 tools {copy,move}`` use
      ``nargs='+'`` and have no ``all`` sentinel); an *empty* list is
      accepted and means "open" (chosen at run time on ``/submit``).
    - ``not spec.uses_transformations``: the formatter/mapper/fixer
      chain is bypassed, so ``transformations`` is scrubbed.
    - ``not spec.uses_cdm``: a stale ``cdm`` value is scrubbed to
      ``None`` (and no CDM-registration check runs).
    - ``spec.health_selection``: the report-cycle ``packages`` list is
      normalised + validated against the registered EDC/DC cycles.
    - When ``spec.uses_cdm`` and ``cdm`` is set, it must reference a CDM
      registered in :mod:`ovid2.cdm.registry`. ``uses_cdm`` with
      ``cdm=None`` is allowed and defers resolution to the pipeline's
      ``resolve_cdm_for_pipeline`` fallback.
    """
    kind = normalize_kind(payload.kind)
    payload.kind = kind
    spec = get_kind(kind)
    if spec is None:
        raise PipelineError(
            f"Unsupported pipeline kind '{kind}'. Supported: {list(PIPELINE_KINDS)}."
        )

    # ``tables`` handling. ``explicit`` kinds (copy / move) forbid the
    # ``['all']`` sentinel — *unless* the input source's driver type can
    # expand ``all`` at run time (e.g. Access, a self-describing
    # container of tables). ``None`` normalises to ``[]`` ("open").
    if spec.tables_mode == "explicit" and payload.tables == ["all"]:
        from ovid2.web.services._table_expand import source_supports_expand_all

        if not source_supports_expand_all(payload.input_address):
            raise PipelineError(
                f"{kind} pipelines do not support `['all']` for this source; "
                f"list the tables explicitly, or leave the list empty to "
                f"choose them at run time (`ovid2 {spec.cli_subcommand}` has "
                f"no `all`)."
            )
    if spec.tables_mode == "explicit":
        payload.tables = list(payload.tables or [])

    # Scrub the formatter/mapper/fixer chain for kinds that bypass it.
    if not spec.uses_transformations:
        payload.transformations = []

    # Scrub a stale ``cdm`` when the kind ignores it (e.g. analysis /
    # copy / move) and skip the CDM-registration check entirely.
    if not spec.uses_cdm:
        payload.cdm = None

    # Copy / move accept none of the etl-only fields — scrub them so the
    # worker's argv builder cannot forward them.
    if not spec.uses_cdm and spec.argv_builder in ("copy", "move"):
        payload.sf_database = None
        payload.sf_account = None
        payload.sf_schema = None
        # ``workers`` stays a hint but is not forwarded to `tools {copy,move}`.

    # Health-shaped kinds carry a report-cycle package selection.
    if spec.health_selection:
        payload.packages = [p.strip() for p in (payload.packages or []) if p.strip()]
        if payload.packages:
            _validate_packages(payload.packages)

    # CDM-registration check (only for kinds that consume a CDM).
    if not spec.uses_cdm:
        return
    if not payload.cdm:
        # Deferred resolution: the pipeline picks its default CDM at
        # runtime. Backwards-compatible with pre-C1 pipelines.
        return
    from ovid2.cdm import registered_cdms  # noqa: PLC0415

    known = set(registered_cdms())
    if payload.cdm not in known:
        raise PipelineError(
            f"Unknown CDM '{payload.cdm}'. Registered: {sorted(known)}."
        )


def _validate_packages(packages: list[str]) -> None:
    """Validate EDC/DC report-cycle package ids against the registry.

    Uses the web-safe :func:`health_report_svc.list_packages` (which reads
    the YAML manifests directly) so the web tier never imports the
    pipeline-tier ``ovid2.transformations`` registry.

    Args:
        packages: The report-cycle package ids to validate (e.g. ``20r1``).

    Raises:
        PipelineError: If any id is not a registered cycle package.
    """
    from ovid2.web.services import health_report_svc  # noqa: PLC0415

    known = {str(p.get("id")) for p in health_report_svc.list_packages()}
    unknown = [p for p in packages if p not in known]
    if unknown:
        raise PipelineError(
            f"Unknown report-cycle package(s): {unknown}. "
            f"Available: {sorted(known) or '(none registered)'}."
        )


def write_pipeline(
    name: str,
    payload: PipelineWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Create or update a pipeline JSON.

    Runtime state (``web_pipelines_state``) is created / refreshed too:

    - New pipeline: an ``enabled`` + ``next_fire_at`` row is inserted.
    - Update: the ``next_fire_at`` value is recomputed from the new
      trigger definition; ``last_fired_at`` is preserved.
    """
    _validate_name(name)
    _validate_write(payload)

    existing_path = _locate(name)
    existing_raw: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise PipelineError(f"Pipeline '{name}' already exists.")
        try:
            existing_raw = _read_raw(existing_path)
        except (OSError, json.JSONDecodeError, PipelineError) as exc:
            raise PipelineError(f"Existing pipeline is not readable: {exc}") from exc

    now_iso = _now_iso()
    doc: dict[str, Any] = {
        "kind": normalize_kind(payload.kind),
        "cdm": payload.cdm,
        "pipeline": payload.pipeline,
        "input_address": payload.input_address,
        "output_address": payload.output_address,
        "tables": list(payload.tables),
        "transformations": list(payload.transformations),
        "packages": list(payload.packages),
        "engine": payload.engine,
        "workers": int(payload.workers),
        "sf_database": payload.sf_database,
        "sf_account": payload.sf_account,
        "sf_schema": payload.sf_schema,
        "stderr_log": bool(payload.stderr_log),
        "enabled": bool(payload.enabled),
        "trigger": payload.trigger.model_dump(exclude_none=True),
        "created_at": (existing_raw or {}).get("created_at", now_iso),
        "updated_at": now_iso,
    }

    target = _writable_path(name)
    before_hash = _hash(existing_raw) if existing_raw is not None else None
    after_hash = _hash(doc)
    action = "updated" if existing_raw is not None else "created"

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)

    # Recompute runtime state.
    prev_state = _read_state(name)
    last_fired = None
    if prev_state and prev_state.get("last_fired_at"):
        try:
            last_fired = datetime.fromisoformat(str(prev_state["last_fired_at"]))
        except ValueError:
            last_fired = None
    next_dt = compute_next_fire(payload.trigger, last_fired_at=last_fired)
    _write_state(
        name,
        enabled=payload.enabled,
        next_fire_at=next_dt.isoformat() if next_dt else None,
    )

    web_db.audit(
        actor=actor,
        action=f"pipeline.{action}",
        target=name,
        before_hash=before_hash,
        after_hash=after_hash,
        detail_json=json.dumps(
            {"path": str(target), "trigger": payload.trigger.type,
             "pipeline": payload.pipeline}
        ),
    )
    logger.info("Pipeline %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def delete_pipeline(name: str, *, actor: str = "web") -> WriteResult:
    """Delete a pipeline JSON and its runtime-state row."""
    _validate_name(name)
    target = _writable_path(name)
    if not target.is_file():
        raise PipelineError(
            f"Pipeline '{name}' not found under {_writable_dir()}."
        )
    before_hash: str | None = None
    try:
        before_hash = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError, PipelineError):
        before_hash = None
    target.unlink()
    _ensure_state_schema()
    with web_db.connect() as conn:
        conn.execute("DELETE FROM web_pipelines_state WHERE name = ?", (name,))
    web_db.audit(
        actor=actor,
        action="pipeline.deleted",
        target=name,
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Pipeline deleted %s at %s", name, target)
    return WriteResult(name=name, path=str(target), action="deleted")


def set_enabled(name: str, enabled: bool, *, actor: str = "web") -> PipelineConfigDetail:
    """Flip a pipeline's ``enabled`` flag (in state + on disk)."""
    detail = get_pipeline(name)
    if detail is None:
        raise PipelineError(f"Pipeline '{name}' not found.")
    payload = PipelineWrite(
        kind=detail.kind,
        cdm=detail.cdm,
        pipeline=detail.pipeline,
        input_address=detail.input_address,
        output_address=detail.output_address,
        tables=list(detail.tables),
        transformations=list(detail.transformations),
        packages=list(detail.packages),
        engine=detail.engine,
        workers=detail.workers,
        sf_database=detail.sf_database,
        sf_account=detail.sf_account,
        sf_schema=detail.sf_schema,
        stderr_log=detail.stderr_log,
        enabled=enabled,
        trigger=detail.trigger,
    )
    write_pipeline(name, payload, actor=actor)
    web_db.audit(
        actor=actor,
        action=f"pipeline.{'enabled' if enabled else 'disabled'}",
        target=name,
    )
    result = get_pipeline(name)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _to_submit_request(
    detail: PipelineConfigDetail,
    *,
    batch_id: str | None = None,
    input_address: str | None = None,
    output_address: str | None = None,
    tables: list[str] | None = None,
) -> JobSubmitRequest:
    """Convert a pipeline detail into a :class:`JobSubmitRequest`.

    When ``batch_id`` is provided (the watch path passes the detected
    folder name) it is used verbatim; otherwise a timestamp batch id is
    synthesised (the manual / cron / interval paths).

    ``input_address`` / ``output_address`` override the config's stored
    address for that side — used when the operator binds an *open*
    (unbound-at-create-time) side for a single run (e.g. the inline
    storage picker on the Pipelines page).

    ``tables`` overrides the config's stored table list for this one run
    — used by the Pipelines "Run" modal so a copy / move pipeline created
    *open* on tables (empty stored list) can supply the explicit list that
    ``ovid2 tools copy|move`` requires. ``None`` (the default) keeps the
    config's stored tables.
    """
    resolved_batch = batch_id or _now().strftime("%Y%m%d-%H%M%S")
    resolved_input = input_address if input_address is not None else detail.input_address
    resolved_output = (
        output_address if output_address is not None else detail.output_address
    )
    resolved_tables = tables if tables is not None else list(detail.tables)
    return JobSubmitRequest(
        kind=detail.kind,
        cdm=detail.cdm,
        pipeline=detail.pipeline,
        batch_id=resolved_batch,
        input_address=resolved_input,
        output_address=resolved_output,
        tables=list(resolved_tables),
        transformations=list(detail.transformations),
        packages=list(detail.packages),
        engine=detail.engine,
        workers=detail.workers,
        sf_database=detail.sf_database,
        sf_account=detail.sf_account,
        sf_schema=detail.sf_schema,
        stderr_log=detail.stderr_log,
    )


def run_now(
    name: str,
    *,
    actor: str = "web",
    batch_id: str | None = None,
    input_address: str | None = None,
    output_address: str | None = None,
    tables: list[str] | None = None,
    draft: bool = False,
) -> JobSubmitResponse:
    """Submit the pipeline as a job right now, regardless of trigger.

    ``draft=True`` stores the job as a ``draft`` (see
    :func:`jobs_svc.create_draft`) instead of queueing it; the dispatch
    is then *not* recorded, because nothing has fired yet.

    ``batch_id`` overrides the synthesised timestamp batch id — the
    watch dispatch path passes the newly-detected input folder name so
    the run reads exactly that batch.

    ``input_address`` / ``output_address`` bind an *open* side for this
    one run (the inline storage picker on the Pipelines page). A side
    that is still empty after applying the override — i.e. the config
    kept it open and the caller supplied nothing — raises
    :class:`PipelineError`, mirroring the Submit form's "storage is
    required" guard.

    ``tables`` overrides the config's stored table list for this one run
    (the Pipelines "Run" modal's tables field). ``None`` keeps the
    config's stored tables. This is how a ``copy`` / ``move`` pipeline
    created *open* on tables supplies the explicit list that
    ``ovid2 tools copy|move`` requires.

    Raises:
        PipelineError: When the pipeline is unknown, an open side is
            left unbound, or the underlying job submission fails
            validation (``jobs_svc.SubmitError`` is re-raised as a
            ``PipelineError`` so callers only handle one type).
    """
    detail = get_pipeline(name)
    if detail is None:
        raise PipelineError(f"Pipeline '{name}' not found.")
    resolved_input = (
        input_address if input_address is not None else detail.input_address
    )
    resolved_output = (
        output_address if output_address is not None else detail.output_address
    )
    if not (resolved_input or "").strip():
        raise PipelineError(
            f"Pipeline '{name}' keeps its input open — choose a storage "
            "account for this run."
        )
    if not (resolved_output or "").strip():
        raise PipelineError(
            f"Pipeline '{name}' keeps its output open — choose a storage "
            "account for this run."
        )
    request = _to_submit_request(
        detail,
        batch_id=batch_id,
        input_address=resolved_input,
        output_address=resolved_output,
        tables=tables,
    )
    # ``jobs_svc.submit`` runs the same submit-time validation as the
    # /submit form (e.g. copy/move jobs reject ``['all']`` tables). It
    # signals those with :class:`jobs_svc.SubmitError`, which is a
    # sibling of :class:`PipelineError` — not a subclass. Translate it
    # here so every ``run_now`` caller (the Pipelines "run" action, the
    # JSON API, the watch dispatcher) sees a single, catchable
    # :class:`PipelineError` and returns a clean 400 instead of letting
    # ``SubmitError`` escape as a 500.
    try:
        if draft:
            return jobs_svc.create_draft(request, actor=actor)
        response = jobs_svc.submit(request, actor=actor)
    except jobs_svc.SubmitError as exc:
        raise PipelineError(str(exc)) from exc
    record_dispatch(name, job_id=response.job_id)
    return response


def record_dispatch(name: str, *, job_id: str) -> None:
    """Record that a pipeline just fired (used by ``run_now`` and the worker)."""
    detail = get_pipeline(name)
    if detail is None:
        return
    fired = _now()
    next_dt = compute_next_fire(detail.trigger, now=fired, last_fired_at=fired)
    _write_state(
        name,
        last_fired_at=fired.isoformat(),
        next_fire_at=next_dt.isoformat() if next_dt else None,
        last_job_id=job_id,
    )


def due_pipelines(*, now: datetime | None = None) -> list[PipelineConfigDetail]:
    """Return every enabled pipeline whose ``next_fire_at`` is due.

    Called by the web-worker on each poll cycle.
    """
    reference = now or _now()
    due: list[PipelineConfigDetail] = []
    for name in _list_names():
        detail = get_pipeline(name)
        if detail is None or not detail.enabled:
            continue
        if detail.trigger.type in ("manual", "watch"):
            # watch is event-driven (dispatched by watch_svc), not the clock.
            continue
        if not detail.next_fire_at:
            # No state yet — compute one and skip this tick.
            next_dt = compute_next_fire(detail.trigger, now=reference)
            _write_state(
                name,
                next_fire_at=next_dt.isoformat() if next_dt else None,
            )
            continue
        try:
            next_dt = datetime.fromisoformat(detail.next_fire_at)
        except ValueError:
            continue
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        if next_dt <= reference:
            due.append(detail)
    return due
