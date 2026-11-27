"""Workflow service — named DAGs of saved pipelines (adr-workflows.md).

A **workflow** chains saved pipeline configs (:mod:`pipelines_svc`) into
a directed acyclic graph of *steps*:

- a ``data`` edge feeds the upstream step's output address into the
  downstream step's input (address = exact location, so the output of
  one run *is* the input of the next);
- an ``after`` edge only orders the steps — the downstream step keeps
  its own source and destination, so independent pipelines with
  different storage can be sequenced.

The workflow owns its trigger (``manual`` / ``cron`` / ``interval`` /
``watch``). A new workflow inherits the first step's pipeline trigger;
a ``watch`` trigger scans the root step's input and starts one run per
new batch folder. The pipelines inside a workflow keep their own
triggers — running one on its own never starts the workflow.

Storage:

- Definitions: ``$XDG_CONFIG_HOME/ovid2/workflows/<name>.json`` (0o644),
  validated against ``resources/schemas/workflow.v1.json``.
- Runtime state: ``web_workflows_state`` (enabled / next fire / last run).

Execution lives in :mod:`workflow_runner_svc`; this module is the
definition side (CRUD, duplicate, validate, schedule bookkeeping).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ovid2 import workspaces as ws_paths
from ovid2.config import addresses as _addresses
from ovid2.run_kinds import get_kind
from ovid2.web import db as web_db
from ovid2.web.models import (
    PipelineTrigger,
    WorkflowDetail,
    WorkflowEdge,
    WorkflowStep,
    WorkflowSummary,
    WorkflowValidation,
    WorkflowWrite,
    WriteResult,
)
from ovid2.web.services import pipelines_svc

logger = logging.getLogger(__name__)

__all__ = [
    "WorkflowError",
    "data_parents",
    "delete_workflow",
    "due_workflows",
    "duplicate_workflow",
    "get_workflow",
    "list_workflows",
    "record_fire",
    "root_steps",
    "set_enabled",
    "topological_order",
    "validate",
    "write_workflow",
]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MAX_STEPS = 50


class WorkflowError(ValueError):
    """Raised when a workflow payload is invalid or targets a bad name."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _search_paths() -> list[Path]:
    """Ordered directories searched for workflow JSONs (workspace-scoped)."""
    return ws_paths.search_dirs("workflows", "OVID2_WORKFLOWS_DIR", include_cwd=False)


def _writable_dir() -> Path:
    """Return (creating) the active workspace's workflow directory."""
    directory = ws_paths.write_dir("workflows", "OVID2_WORKFLOWS_DIR")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _validate_name(name: str) -> None:
    if not name or not _NAME_RE.match(name):
        raise WorkflowError(
            f"Invalid workflow name '{name}'. Use letters, digits, '-', '_', '.' "
            "(start with a letter or digit, max 64)."
        )


def _locate(name: str) -> Path | None:
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def _list_names() -> list[str]:
    seen: set[str] = set()
    for directory in _search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith("."):
                seen.add(entry.stem)
    return sorted(seen)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _schema_validator() -> Any:
    import jsonschema

    text = (resources.files("ovid2.resources.schemas") / "workflow.v1.json").read_text(
        encoding="utf-8"
    )
    return jsonschema.Draft202012Validator(json.loads(text))


def _to_doc(payload: WorkflowWrite) -> dict[str, Any]:
    """Serialise a write payload to the on-disk document shape."""
    return {
        "v": 1,
        "description": payload.description,
        "enabled": bool(payload.enabled),
        "trigger": payload.trigger.model_dump(exclude_none=True),
        "steps": [_step_doc(s) for s in payload.steps],
        "edges": [_edge_doc(e) for e in payload.edges],
    }


def _step_doc(step: WorkflowStep) -> dict[str, Any]:
    """A step as stored: branching keys only when they differ from the default."""
    doc = step.model_dump(exclude_none=True, exclude={"join", "probes"})
    if step.join != "all":
        doc["join"] = step.join
    if step.probes:
        doc["probes"] = [p.model_dump(exclude_none=True) for p in step.probes]
    return doc


def _edge_doc(edge: WorkflowEdge) -> dict[str, Any]:
    """An edge as stored: ``on`` / ``when`` / ``label`` only when set."""
    doc: dict[str, Any] = {"from": edge.source, "to": edge.target, "type": edge.type}
    if edge.on != "success":
        doc["on"] = edge.on
    if edge.when is not None:
        doc["when"] = edge.when
    if edge.label:
        doc["label"] = edge.label
    return doc


def _schema_errors(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for err in sorted(_schema_validator().iter_errors(doc), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def topological_order(steps: list[WorkflowStep], edges: list[WorkflowEdge]) -> list[str] | None:
    """Return a topological order of step ids, or ``None`` when there is a cycle."""
    ids = [s.id for s in steps]
    indeg = dict.fromkeys(ids, 0)
    children: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        if e.source in indeg and e.target in indeg:
            indeg[e.target] += 1
            children[e.source].append(e.target)
    ready = [i for i in ids if indeg[i] == 0]
    out: list[str] = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for child in children[node]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    return out if len(out) == len(ids) else None


def root_steps(steps: list[WorkflowStep], edges: list[WorkflowEdge]) -> list[str]:
    """Return the ids of steps with no incoming edge (in step order)."""
    targets = {e.target for e in edges}
    return [s.id for s in steps if s.id not in targets]


def data_parents(step_id: str, edges: list[WorkflowEdge]) -> list[str]:
    """Return the upstream step ids feeding ``step_id`` over ``data`` edges."""
    return [e.source for e in edges if e.target == step_id and e.type == "data"]


def _upstream(step_id: str, edges: list[WorkflowEdge]) -> list[str]:
    return [e.source for e in edges if e.target == step_id]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _address_rollback_capable(address: str) -> bool | None:
    """``True`` / ``False`` from the address's driver type, ``None`` when unknown."""
    from ovid2.web.services._table_expand import _driver_type_for_address

    driver_type = _driver_type_for_address(address)
    if not driver_type:
        return None
    try:
        from ovid2.drivers.registry import load_class

        return bool(load_class(driver_type).supports_workflow_rollback())
    except (KeyError, ImportError):
        return None


def validate(payload: WorkflowWrite, *, check_trigger: bool = True) -> WorkflowValidation:
    """Validate a workflow definition without writing it.

    Checks the JSON schema, step ids, edge endpoints, acyclicity, that
    every referenced pipeline exists, data-edge kind compatibility, that
    every step ends up with an input and an output, that no two steps
    write the same output, that rollback-enabled steps write to a store
    that supports it, and the trigger.
    """
    errors: list[str] = []
    warnings: list[str] = []
    step_errors: dict[str, list[str]] = {}

    def step_err(step_id: str, msg: str) -> None:
        step_errors.setdefault(step_id, []).append(msg)
        errors.append(f"step '{step_id}': {msg}")

    errors.extend(_schema_errors(_to_doc(payload)))
    if not payload.steps:
        errors.append("A workflow needs at least one step.")
    if len(payload.steps) > _MAX_STEPS:
        errors.append(f"A workflow may have at most {_MAX_STEPS} steps.")

    ids: list[str] = []
    for s in payload.steps:
        if not _STEP_ID_RE.match(s.id):
            errors.append(f"Invalid step id '{s.id}' (lower-case letters, digits, '-', '_').")
        if s.id in ids:
            errors.append(f"Duplicate step id '{s.id}'.")
        ids.append(s.id)
    id_set = set(ids)

    seen_edges: set[tuple[str, str]] = set()
    for e in payload.edges:
        if e.source not in id_set or e.target not in id_set:
            errors.append(f"Edge {e.source} → {e.target} references an unknown step.")
            continue
        if e.source == e.target:
            errors.append(f"Edge {e.source} → {e.target} links a step to itself.")
        if (e.source, e.target) in seen_edges:
            errors.append(f"Duplicate edge {e.source} → {e.target}.")
        seen_edges.add((e.source, e.target))

    _validate_branches(payload, id_set, errors, warnings, step_err)

    order = topological_order(payload.steps, payload.edges)
    if order is None:
        errors.append("The workflow has a cycle; steps must form a DAG.")
        order = []

    configs = {s.id: pipelines_svc.get_pipeline(s.pipeline) for s in payload.steps}
    resolved_out: dict[str, str] = {}
    for s in payload.steps:
        detail = configs[s.id]
        if detail is None:
            step_err(s.id, f"pipeline '{s.pipeline}' not found.")
            continue
        resolved_out[s.id] = (s.overrides.output_address or detail.output_address or "").strip()

    known = set(_addresses.list_available_addresses())
    roots = set(root_steps(payload.steps, payload.edges))
    outputs: dict[str, str] = {}
    for s in payload.steps:
        detail = configs.get(s.id)
        if detail is None:
            continue
        spec = get_kind(detail.kind)
        parents = data_parents(s.id, payload.edges)
        if len(parents) > 1 and s.join != "any":
            step_err(
                s.id,
                "has more than one incoming data link; a step reads one input — set its "
                "join to 'any' so it reads from whichever branch ran.",
            )
        if spec is not None and spec.job_only:
            step_err(s.id, f"kind '{detail.kind}' cannot run in a workflow.")
        for p in parents:
            pdetail = configs.get(p)
            if pdetail is None:
                continue
            if not resolved_out.get(p):
                step_err(s.id, f"its data input comes from '{p}', which has no output address.")
        if parents:
            if s.overrides.input_address:
                warnings.append(
                    f"step '{s.id}': input override is ignored — the data edge from "
                    f"'{parents[0]}' supplies the input."
                )
        else:
            inp = (s.overrides.input_address or detail.input_address or "").strip()
            if not inp and not (s.id in roots and payload.trigger.type == "watch"):
                step_err(s.id, "has no input address (bind one in the step or the pipeline).")
            if inp and known and inp not in known:
                step_err(s.id, f"input address '{inp}' does not exist.")
        out = resolved_out.get(s.id, "")
        if not out:
            step_err(s.id, "has no output address (bind one in the step or the pipeline).")
        elif known and out not in known:
            step_err(s.id, f"output address '{out}' does not exist.")
        elif out in outputs:
            step_err(s.id, f"writes to '{out}', the same output as step '{outputs[out]}'.")
        else:
            outputs[out] = s.id
        if out and s.rollback:
            capable = _address_rollback_capable(out)
            if capable is False:
                step_err(
                    s.id,
                    f"output '{out}' is on a storage type that cannot be rolled back; "
                    "turn rollback off for this step.",
                )
        tables_mode = spec.tables_mode if spec else "all"
        if (
            tables_mode == "explicit"
            and parents
            and not (s.overrides.tables or detail.tables)
            and not _source_can_expand(resolved_out.get(parents[0], ""))
        ):
            warnings.append(
                f"step '{s.id}': copy / move with no table list reads every table its "
                "upstream step wrote."
            )

    if payload.trigger.type == "watch":
        root_list = root_steps(payload.steps, payload.edges)
        if len(root_list) != 1:
            errors.append("A watch trigger needs exactly one root step (the one that is watched).")
        elif check_trigger:
            root = next(s for s in payload.steps if s.id == root_list[0])
            rdetail = configs.get(root.id)
            watched = (
                root.overrides.input_address or (rdetail.input_address if rdetail else "") or ""
            )
            try:
                pipelines_svc._validate_trigger(payload.trigger, input_address=watched)
            except pipelines_svc.PipelineError as exc:
                errors.append(f"trigger: {exc}")
    elif check_trigger:
        try:
            pipelines_svc._validate_trigger(payload.trigger)
        except pipelines_svc.PipelineError as exc:
            errors.append(f"trigger: {exc}")

    return WorkflowValidation(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        step_errors=step_errors,
        order=order,
    )


def _validate_branches(
    payload: WorkflowWrite,
    id_set: set[str],
    errors: list[str],
    warnings: list[str],
    step_err: Any,
) -> None:
    """Check edge conditions, join modes and data probes (adr-workflows.md, Branching)."""
    from ovid2.config import workflow_conditions as wc

    probes_by_step: dict[str, set[str]] = {}
    for s in payload.steps:
        names: set[str] = set()
        if len(s.probes) > wc.MAX_PROBES:
            step_err(s.id, f"declares more than {wc.MAX_PROBES} data probes.")
        for i, probe in enumerate(s.probes):
            for msg in wc.validate_probe(
                probe.model_dump(exclude_none=True), where=f"probe {i + 1}"
            ):
                step_err(s.id, msg)
            if probe.name in names:
                step_err(s.id, f"two data probes are named '{probe.name}'.")
            names.add(probe.name)
        probes_by_step[s.id] = names

    by_source: dict[str, list[WorkflowEdge]] = {}
    for e in payload.edges:
        if e.source not in id_set or e.target not in id_set:
            continue
        by_source.setdefault(e.source, []).append(e)
        where = f"link {e.source} → {e.target}"
        if e.when is not None:
            if e.on == "otherwise":
                errors.append(f"{where}: an 'otherwise' link cannot have its own condition.")
            for msg in wc.validate_condition(
                e.when, probes=probes_by_step.get(e.source), where=where
            ):
                errors.append(msg)
            groups = wc.field_groups(e.when)
            if "query" in groups and e.on == "failure":
                warnings.append(f"{where}: data probes only run when the step succeeds.")
    for src, out in by_source.items():
        n_other = sum(1 for e in out if e.on == "otherwise")
        if n_other > 1:
            errors.append(f"step '{src}' has more than one 'otherwise' link.")
        if n_other and not any(e.on == "success" and e.when is not None for e in out):
            warnings.append(
                f"step '{src}': its 'otherwise' link has no conditional success link to be the "
                "else of, so it always runs on success."
            )

    for s in payload.steps:
        incoming = [e for e in payload.edges if e.target == s.id]
        if s.join == "any" and len(incoming) < 2:
            warnings.append(
                f"step '{s.id}': join 'any' only matters with two or more incoming links."
            )


def _source_can_expand(address: str) -> bool:
    if not address:
        return False
    from ovid2.web.services._table_expand import source_supports_expand_all

    return source_supports_expand_all(address)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _read_state(name: str) -> dict[str, Any]:
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_workflows_state WHERE name = ?", (web_db.state_key(name),)
        ).fetchone()
    return dict(row) if row is not None else {}


def _write_state(name: str, **fields: Any) -> None:
    web_db.ensure_schema()
    current = _read_state(name)
    merged = {
        "enabled": 1,
        "last_fired_at": None,
        "next_fire_at": None,
        "last_run_id": None,
        **current,
    }
    for key, value in fields.items():
        if key == "enabled":
            merged["enabled"] = 1 if value else 0
        elif value is not None or key == "next_fire_at":
            merged[key] = value
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_workflows_state (name, enabled, last_fired_at, next_fire_at, "
            "last_run_id) VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "enabled = excluded.enabled, last_fired_at = excluded.last_fired_at, "
            "next_fire_at = excluded.next_fire_at, last_run_id = excluded.last_run_id",
            (
                web_db.state_key(name),
                merged["enabled"],
                merged["last_fired_at"],
                merged["next_fire_at"],
                merged["last_run_id"],
            ),
        )


def _last_run_status(run_id: str | None) -> str | None:
    if not run_id:
        return None
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM web_workflow_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, ws_paths.active_workspace()),
        ).fetchone()
    return str(row["status"]) if row is not None else None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _read_doc(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise WorkflowError(f"Workflow file {path} is not a JSON object.")
    return data


def _payload_from_doc(doc: dict[str, Any]) -> WorkflowWrite:
    return WorkflowWrite.model_validate(
        {
            "description": doc.get("description") or "",
            "enabled": bool(doc.get("enabled", True)),
            "trigger": doc.get("trigger") or {"type": "manual"},
            "steps": doc.get("steps") or [],
            "edges": doc.get("edges") or [],
        }
    )


def get_workflow(name: str) -> WorkflowDetail | None:
    """Return one workflow (definition + runtime state), or ``None``."""
    path = _locate(name)
    if path is None:
        return None
    try:
        doc = _read_doc(path)
        payload = _payload_from_doc(doc)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Unreadable workflow %s: %s", path, exc)
        return None
    state = _read_state(name)
    enabled = bool(state.get("enabled", 1)) if state else payload.enabled
    return WorkflowDetail(
        **payload.model_dump(exclude={"enabled"}),
        enabled=enabled,
        name=name,
        path=str(path),
        root_steps=root_steps(payload.steps, payload.edges),
        last_fired_at=state.get("last_fired_at"),
        next_fire_at=state.get("next_fire_at"),
        last_run_id=state.get("last_run_id"),
        last_run_status=_last_run_status(state.get("last_run_id")),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def list_workflows() -> list[WorkflowSummary]:
    """Return every workflow on the search path."""
    out: list[WorkflowSummary] = []
    for name in _list_names():
        detail = get_workflow(name)
        if detail is None:
            continue
        out.append(
            WorkflowSummary(
                name=name,
                description=detail.description,
                enabled=detail.enabled,
                trigger_type=detail.trigger.type,
                step_count=len(detail.steps),
                edge_count=len(detail.edges),
                steps=detail.steps,
                edges=detail.edges,
                next_fire_at=detail.next_fire_at,
                last_fired_at=detail.last_fired_at,
                last_run_id=detail.last_run_id,
                last_run_status=detail.last_run_status,
                updated_at=detail.updated_at,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_workflow(
    name: str,
    payload: WorkflowWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Validate and persist a workflow definition.

    Raises:
        WorkflowError: On an invalid name or definition, or (with
            ``create_only``) when the workflow already exists.
    """
    _validate_name(name)
    result = validate(payload)
    if not result.ok:
        raise WorkflowError("; ".join(result.errors))
    existing_path = _locate(name)
    existing: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise WorkflowError(f"Workflow '{name}' already exists.")
        try:
            existing = _read_doc(existing_path)
        except (OSError, json.JSONDecodeError, WorkflowError):
            existing = None
    now_iso = _now().isoformat()
    doc = _to_doc(payload)
    doc["created_at"] = (existing or {}).get("created_at", now_iso)
    doc["updated_at"] = now_iso
    target = _writable_dir() / f"{name}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)

    state = _read_state(name)
    last = state.get("last_fired_at")
    last_dt = datetime.fromisoformat(last) if last else None
    next_dt = pipelines_svc.compute_next_fire(payload.trigger, last_fired_at=last_dt)
    _write_state(
        name, enabled=payload.enabled, next_fire_at=next_dt.isoformat() if next_dt else None
    )
    action = "updated" if existing is not None else "created"
    web_db.audit(
        actor=actor,
        action=f"workflow.{action}",
        target=name,
        before_hash=_hash(existing) if existing else None,
        after_hash=_hash(doc),
        detail_json=json.dumps(
            {
                "steps": len(payload.steps),
                "edges": len(payload.edges),
                "trigger": payload.trigger.type,
            }
        ),
    )
    logger.info("Workflow %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def delete_workflow(name: str, *, actor: str = "web") -> WriteResult:
    """Delete a workflow definition and its runtime state (run history is kept).

    Raises:
        WorkflowError: When the workflow does not exist in the user directory
            or has a run in progress.
    """
    _validate_name(name)
    target = _writable_dir() / f"{name}.json"
    if not target.is_file():
        raise WorkflowError(f"Workflow '{name}' not found under {_writable_dir()}.")
    from ovid2.web.services import workflow_runner_svc

    if workflow_runner_svc.active_run(name) is not None:
        raise WorkflowError(f"Workflow '{name}' has a run in progress; cancel it first.")
    before = _hash(_read_doc(target))
    target.unlink()
    with web_db.connect() as conn:
        conn.execute("DELETE FROM web_workflows_state WHERE name = ?", (web_db.state_key(name),))
        conn.execute(
            "DELETE FROM web_watch_cursors WHERE pipeline = ?",
            (web_db.state_key(f"workflow:{name}"),),
        )
    web_db.audit(actor=actor, action="workflow.deleted", target=name, before_hash=before)
    return WriteResult(name=name, path=str(target), action="deleted")


def duplicate_workflow(
    name: str, new_name: str | None = None, *, actor: str = "web"
) -> WriteResult:
    """Copy a workflow under a new name. The copy starts disabled.

    Raises:
        WorkflowError: When the source is missing or the target exists.
    """
    detail = get_workflow(name)
    if detail is None:
        raise WorkflowError(f"Workflow '{name}' not found.")
    target = (new_name or "").strip()
    if not target:
        base = f"{name}_copy"
        target, n = base, 2
        while _locate(target) is not None:
            target = f"{base}{n}"
            n += 1
    payload = WorkflowWrite(
        description=detail.description,
        enabled=False,
        trigger=detail.trigger,
        steps=detail.steps,
        edges=detail.edges,
    )
    result = write_workflow(target, payload, actor=actor, create_only=True)
    web_db.audit(actor=actor, action="workflow.duplicated", target=target, detail_json=name)
    return result


def set_enabled(name: str, enabled: bool, *, actor: str = "web") -> WorkflowDetail:
    """Enable / disable a workflow's trigger."""
    detail = get_workflow(name)
    if detail is None:
        raise WorkflowError(f"Workflow '{name}' not found.")
    path = Path(detail.path)
    doc = _read_doc(path)
    doc["enabled"] = bool(enabled)
    if path.parent == _writable_dir():
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    next_dt = pipelines_svc.compute_next_fire(detail.trigger) if enabled else None
    _write_state(name, enabled=enabled, next_fire_at=next_dt.isoformat() if next_dt else None)
    web_db.audit(
        actor=actor, action=f"workflow.{'enabled' if enabled else 'disabled'}", target=name
    )
    out = get_workflow(name)
    assert out is not None
    return out


def default_trigger_for(pipeline_name: str) -> PipelineTrigger:
    """Return the trigger a new workflow inherits from its first pipeline."""
    detail = pipelines_svc.get_pipeline(pipeline_name)
    return detail.trigger if detail is not None else PipelineTrigger(type="manual")


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def record_fire(name: str, *, run_id: str) -> None:
    """Record that a workflow just started a run."""
    detail = get_workflow(name)
    if detail is None:
        return
    fired = _now()
    next_dt = pipelines_svc.compute_next_fire(detail.trigger, now=fired, last_fired_at=fired)
    _write_state(
        name,
        last_fired_at=fired.isoformat(),
        next_fire_at=next_dt.isoformat() if next_dt else None,
        last_run_id=run_id,
    )


def due_workflows(*, now: datetime | None = None) -> list[WorkflowDetail]:
    """Return enabled clock-triggered workflows whose ``next_fire_at`` has passed."""
    reference = now or _now()
    due: list[WorkflowDetail] = []
    for name in _list_names():
        detail = get_workflow(name)
        if detail is None or not detail.enabled or detail.trigger.type in ("manual", "watch"):
            continue
        if not detail.next_fire_at:
            nxt = pipelines_svc.compute_next_fire(detail.trigger, now=reference)
            _write_state(name, next_fire_at=nxt.isoformat() if nxt else None)
            continue
        try:
            nxt_dt = datetime.fromisoformat(detail.next_fire_at)
        except ValueError:
            continue
        if nxt_dt.tzinfo is None:
            nxt_dt = nxt_dt.replace(tzinfo=UTC)
        if nxt_dt <= reference:
            due.append(detail)
    return due


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web tier.")
