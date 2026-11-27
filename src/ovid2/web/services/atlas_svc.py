"""Ovid Atlas — one workspace-wide graph of storage ⟂ compute ⟂ algorithms.

The Atlas (``/navigator``) draws the whole system setup of the active
workspace as three orthogonal layers:

* **storage** — storage accounts (connections) and the addresses (exact
  locations) defined on them;
* **compute** — engines (named engine configs and the bare engine types a
  pipeline uses);
* **algorithm** — pipeline packages (formatter → … → fix chains) and the
  saved pipeline configs (run configs) that bind a package to an input
  address, an engine and an output address.

A run config is drawn as one *pipeline component*: its name, its shape
(run kind · package), its material (engine type) and its input / output
addresses. A workflow is a DAG of those components: ``workflow.meta``
carries the steps (with their address overrides) and the step-id edges.

Everything here is **static**: JSON configs, YAML specs and ``ast`` — never
a driver, engine or pipeline import (``tests/contract/test_tier_import_closure.py``)
and never a secret value. Live state (which beams / tables / stages are
running) comes from :func:`live_state`, which reads ``web_jobs`` and the
saved ``web_run_items`` rows plus the tail of each running job's event log;
overlays (:func:`heat`, :func:`health_overlay`, :func:`config_drift`,
:func:`impact`) are aggregates over the same data.

ADR: the adr-atlas design note.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.web import db as web_db
from ovid2.workspaces import active_workspace

logger = logging.getLogger(__name__)

#: The three orthogonal layers, bottom → top.
LAYERS: tuple[str, ...] = ("storage", "compute", "algorithm")

#: Node kinds and the layer each lives on. Pinned by
#: ``tests/contract/test_atlas_catalogue.py`` against the renderer table in
#: ``static/atlas/kinds.js``.
NODE_KINDS: dict[str, str] = {
    "account": "storage",
    "address": "storage",
    "engine": "compute",
    "package": "algorithm",
    "config": "algorithm",
    "workflow": "algorithm",
}

#: Edge kinds: ``reads`` (address → config), ``writes`` (config → address),
#: ``runs_on`` (config → engine), ``implements`` (package → config),
#: ``holds`` (account → address), ``step`` (workflow → config),
#: ``flows`` (config → config, a workflow ``data`` / ``after`` link).
EDGE_KINDS: tuple[str, ...] = (
    "reads",
    "writes",
    "runs_on",
    "implements",
    "holds",
    "step",
    "flows",
)

#: Job statuses that are still live.
_LIVE_JOB = ("queued", "running")

#: Pipeline stages in chain order (mirrors ``ovid2.core.events.PIPELINE_STAGES``;
#: duplicated so this module never imports ``ovid2.core``).
STAGES: tuple[str, ...] = ("read", "format", "deduplicate", "transform", "map", "fix", "write")


def node_id(kind: str, name: str) -> str:
    """Return the stable id of a node (``<kind>:<name>``)."""
    return f"{kind}:{name}"


# ---------------------------------------------------------------------------
# Static graph
# ---------------------------------------------------------------------------


def _safe(fn: Any, default: Any) -> Any:
    """Call ``fn()``; log and return ``default`` on any error (never 500 the page)."""
    try:
        return fn()
    except Exception as exc:  # one broken config must not hide the whole Atlas
        logger.warning("atlas: %s failed: %s", getattr(fn, "__name__", fn), exc)
        return default


def _connections() -> list[dict[str, Any]]:
    from ovid2.web.services import connections_svc

    return [{"name": c.name, "type": c.type} for c in _safe(connections_svc.list_connections, [])]


def _addresses() -> list[dict[str, Any]]:
    from ovid2.web.services import addresses_svc

    out: list[dict[str, Any]] = []
    for summary in _safe(addresses_svc.list_addresses, []):
        detail = _safe(lambda s=summary: addresses_svc.get_address(s.name), None)
        location = dict(detail.location or {}) if detail is not None and detail.location else {}
        out.append(
            {
                "name": summary.name,
                "src_name": summary.src_name,
                "connection": summary.connection,
                "location": location,
            }
        )
    return out


def _engines() -> list[dict[str, Any]]:
    from ovid2.web.services import engines_svc

    return [{"name": e.name, "type": e.type} for e in _safe(engines_svc.list_engines, [])]


def _configs() -> list[Any]:
    from ovid2.web.services import pipelines_svc

    return list(_safe(pipelines_svc.list_pipelines, []))


def _workflows() -> list[Any]:
    from ovid2.web.services import workflows_svc

    return list(_safe(workflows_svc.list_workflows, []))


def _packages(used: set[str]) -> list[dict[str, Any]]:
    """Return the facts of every package a config uses (plus template sources)."""
    from ovid2.web.services import pipeline_lineage_svc

    tree = _safe(lambda: pipeline_lineage_svc.navigator_tree("cdm"), {"groups": []})
    out: dict[str, dict[str, Any]] = {}
    for group in tree.get("groups", []):
        for pkg in group.get("pipelines", []):
            out[str(pkg["pipeline"])] = pkg
    for name in sorted(used - set(out)):
        facts = _safe(lambda n=name: pipeline_lineage_svc._package_facts(n), {})
        if facts:
            out[name] = {**facts, "configs": [], "unused": False, "protected": False}
    return [out[k] for k in sorted(out)]


def _engine_capabilities(engine_type: str | None) -> list[str]:
    from ovid2.config.capabilities import engine_capabilities

    caps = engine_capabilities(engine_type or "") if engine_type else None
    return sorted(c.value for c in caps) if caps else []


def _engine_family(engine_type: str | None) -> str:
    from ovid2.config.engines import engine_family

    return (engine_family(engine_type) or "") if engine_type else ""


def _location_label(location: dict[str, Any]) -> str:
    for key in ("path", "schema", "container", "prefix", "database"):
        value = location.get(key)
        if value:
            if key == "schema" and location.get("database"):
                return f"{location['database']}/{value}"
            return str(value)
    return "/"


def _read_only_types(types: set[str]) -> set[str]:
    """Return the driver types among ``types`` that are read-only.

    ``Driver.read_only`` is a class attribute (Access); the same lazy class
    load the submit pre-flight does (:mod:`ovid2.drivers.engine_compat`).
    """
    from ovid2.drivers.registry import DRIVERS, load_class

    out: set[str] = set()
    for type_name in types & set(DRIVERS):
        cls = _safe(lambda t=type_name: load_class(t), None)
        if cls is not None and bool(getattr(cls, "read_only", False)):
            out.add(type_name)
    return out


def build_graph() -> dict[str, Any]:
    """Return the static Atlas graph of the active workspace.

    Returns:
        ``{workspace, layers, nodes, edges, generated_at, digest}``. Every
        node is ``{id, kind, layer, name, label, meta, parent?}``; every edge
        ``{id, kind, source, target, meta}``. ``digest`` changes whenever the
        graph does (the browser re-lays out only then).
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add(
        kind: str, name: str, label: str, meta: dict[str, Any], parent: str | None = None
    ) -> str:
        nid = node_id(kind, name)
        if nid not in nodes:
            node: dict[str, Any] = {
                "id": nid,
                "kind": kind,
                "layer": NODE_KINDS[kind],
                "name": name,
                "label": label,
                "meta": meta,
            }
            if parent:
                node["parent"] = parent
            nodes[nid] = node
        return nid

    def link(kind: str, source: str, target: str, meta: dict[str, Any] | None = None) -> None:
        edges.append(
            {
                "id": f"{kind}:{source}->{target}",
                "kind": kind,
                "source": source,
                "target": target,
                "meta": meta or {},
            }
        )

    connections = _connections()
    read_only = _read_only_types({c["type"] for c in connections})
    conn_types: dict[str, str] = {}
    for conn in connections:
        conn_types[conn["name"]] = conn["type"]
        add(
            "account",
            conn["name"],
            conn["name"],
            {
                "type": conn["type"],
                "read_only": conn["type"] in read_only,
                "dataspace_path": "/" + conn["name"],
            },
        )

    from ovid2.web.services import output_probe_svc

    for addr in _addresses():
        conn = addr["connection"]
        parent = node_id("account", conn) if conn and node_id("account", conn) in nodes else None
        meta = {
            "connection": conn,
            "type": conn_types.get(conn or "", addr["src_name"]),
            "location": addr["location"],
            "legacy": not conn,
            "read_only": conn_types.get(conn or "", "") in read_only,
        }
        dsp = _safe(lambda a=addr["name"]: output_probe_svc.batch_id_for_address(a), None)
        if dsp:
            meta["dataspace_path"] = "/" + dsp
        nid = add(
            "address",
            addr["name"],
            _location_label(addr["location"]) if conn else addr["name"],
            meta,
            parent,
        )
        if parent:
            link("holds", parent, nid)

    for eng in _engines():
        add(
            "engine",
            eng["name"],
            eng["name"],
            {
                "type": eng["type"],
                "family": _engine_family(eng["type"]),
                "capabilities": _engine_capabilities(eng["type"]),
                "configured": True,
            },
        )

    configs = _configs()
    used = {c.pipeline for c in configs if c.pipeline}
    pkg_index: dict[str, dict[str, Any]] = {}
    for pkg in _packages(used):
        pkg_index[pkg["pipeline"]] = pkg
        add(
            "package",
            pkg["pipeline"],
            pkg["pipeline"],
            {
                "cdm": pkg.get("cdm", ""),
                "profile": pkg.get("profile", ""),
                "template": pkg.get("template", ""),
                "editable": bool(pkg.get("editable", True)),
                "formatted_tables": int(pkg.get("formatted_tables", 0) or 0),
                "has_transforms": bool(pkg.get("has_transforms")),
                "fixer_source": pkg.get("fixer_source", ""),
                "unused": bool(pkg.get("unused")),
            },
        )

    from ovid2.web.kinds import get_kind

    for cfg in configs:
        spec = get_kind(cfg.kind)
        engine_name = cfg.engine or "pyspark"
        engine_type = cfg.engine_type
        eid = node_id("engine", engine_name)
        if eid not in nodes:
            add(
                "engine",
                engine_name,
                engine_name,
                {
                    "type": engine_type or engine_name,
                    "family": _engine_family(engine_type),
                    "capabilities": _engine_capabilities(engine_type),
                    "configured": False,
                },
            )
        cid = add(
            "config",
            cfg.name,
            cfg.name,
            {
                "kind": cfg.kind,
                "kind_label": spec.label if spec else cfg.kind,
                "pipeline": cfg.pipeline,
                "cdm": cfg.cdm,
                "input_cdm": cfg.input_cdm,
                "engine": engine_name,
                "engine_type": engine_type,
                "input_address": cfg.input_address,
                "output_address": cfg.output_address,
                "tables": list(cfg.tables),
                "trigger": cfg.trigger_type,
                "enabled": cfg.enabled,
                "next_fire_at": cfg.next_fire_at,
                "last_fired_at": cfg.last_fired_at,
                "description": cfg.description,
                "package_required": bool(spec.package_required) if spec else True,
                "uses_transformations": bool(spec.uses_transformations) if spec else True,
            },
        )
        link("runs_on", cid, eid)
        pid = node_id("package", cfg.pipeline)
        if pid in nodes:
            link("implements", pid, cid)
        for role, name in (("reads", cfg.input_address), ("writes", cfg.output_address)):
            if not name:
                continue
            aid = node_id("address", name)
            if aid not in nodes:
                add("address", name, name, {"missing": True, "type": "", "location": {}})
            if role == "reads":
                link("reads", aid, cid)
            else:
                link("writes", cid, aid)

    for wf in _workflows():
        wid = add(
            "workflow",
            wf.name,
            wf.name,
            {
                "enabled": wf.enabled,
                "trigger": wf.trigger_type,
                "steps": [
                    {
                        "id": s.id,
                        "pipeline": s.pipeline,
                        "label": s.label,
                        "join": s.join,
                        "input_address": s.overrides.input_address,
                        "output_address": s.overrides.output_address,
                    }
                    for s in wf.steps
                ],
                # The DAG between steps (step ids), drawn by the lane board.
                "edges": [
                    {"from": e.source, "to": e.target, "type": e.type, "on": e.on} for e in wf.edges
                ],
                "last_run_id": wf.last_run_id,
                "last_run_status": wf.last_run_status,
            },
        )
        step_cfg = {s.id: s.pipeline for s in wf.steps}
        for step in wf.steps:
            cid = node_id("config", step.pipeline)
            if cid in nodes:
                link("step", wid, cid, {"step": step.id})
        for e in wf.edges:
            a, b = step_cfg.get(e.source), step_cfg.get(e.target)
            if a and b:
                link(
                    "flows",
                    node_id("config", a),
                    node_id("config", b),
                    {"workflow": wf.name, "type": e.type, "on": e.on},
                )

    node_list = sorted(
        nodes.values(), key=lambda n: (LAYERS.index(n["layer"]), n["kind"], n["name"])
    )
    digest = hashlib.sha256(
        json.dumps([node_list, edges], sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return {
        "workspace": active_workspace(),
        "layers": list(LAYERS),
        "node_kinds": dict(NODE_KINDS),
        "stages": list(STAGES),
        "nodes": node_list,
        "edges": edges,
        "digest": digest,
        "generated_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Node detail (lazy expand)
# ---------------------------------------------------------------------------


def node_detail(nid: str) -> dict[str, Any]:
    """Return the expand-in-place detail of one node.

    * ``package`` → per-table lineage summary (stages, ops, fields counts);
    * ``config`` → the saved config, its last runs, the per-table plan;
    * ``address`` → the address (masked) + its Data Space path;
    * ``account`` / ``engine`` / ``workflow`` → the saved definition.

    Raises:
        LookupError: Unknown node id / missing resource.
    """
    kind, _, name = nid.partition(":")
    if kind not in NODE_KINDS or not name:
        raise LookupError(f"Unknown Atlas node '{nid}'.")
    if kind == "package":
        from ovid2.web.services import pipeline_lineage_svc

        return {"id": nid, "kind": kind, "lineage": pipeline_lineage_svc.package_lineage(name)}
    if kind == "config":
        from ovid2.web.services import jobs_svc, pipelines_svc

        detail = pipelines_svc.get_pipeline(name)
        if detail is None:
            raise LookupError(f"Pipeline '{name}' not found.")
        jobs = jobs_svc.list_jobs(limit=8, pipeline=detail.pipeline).jobs
        return {
            "id": nid,
            "kind": kind,
            "config": detail.model_dump(),
            "recent_jobs": [job_brief(j) for j in jobs],
        }
    if kind == "address":
        from ovid2.web.services import addresses_svc, output_probe_svc

        addr = addresses_svc.get_address(name)
        if addr is None:
            raise LookupError(f"Address '{name}' not found.")
        dsp = output_probe_svc.batch_id_for_address(name)
        return {
            "id": nid,
            "kind": kind,
            "address": addr.model_dump(exclude={"path"}),
            "dataspace_path": "/" + dsp if dsp else None,
        }
    if kind == "account":
        from ovid2.web.services import connections_svc

        conn = connections_svc.get_connection(name)
        if conn is None:
            raise LookupError(f"Storage account '{name}' not found.")
        return {
            "id": nid,
            "kind": kind,
            "connection": conn.model_dump(exclude={"path", "secrets"}),
            "dataspace_path": "/" + name,
        }
    if kind == "engine":
        from ovid2.web.services import engines_svc

        eng = engines_svc.get_engine(name)
        if eng is None:
            return {"id": nid, "kind": kind, "engine": {"name": name, "type": name, "args": {}}}
        return {"id": nid, "kind": kind, "engine": eng.model_dump(exclude={"path", "secrets"})}
    from ovid2.web.services import workflows_svc

    wf = workflows_svc.get_workflow(name)
    if wf is None:
        raise LookupError(f"Workflow '{name}' not found.")
    return {"id": nid, "kind": kind, "workflow": wf.model_dump(by_alias=True, exclude={"path"})}


def job_brief(job: Any) -> dict[str, Any]:
    """Return the browser-facing summary of one :class:`JobDetail`."""
    return {
        "job_id": job.job_id,
        "instance_id": job.instance_id,
        "status": job.status,
        "pause_requested": job.pause_requested,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "error": job.error,
        "pipeline": job.pipeline,
        "input_address": job.input_address,
        "output_address": job.output_address,
    }


# ---------------------------------------------------------------------------
# Live state
# ---------------------------------------------------------------------------


def _config_for_job(job: Any, configs: list[Any]) -> str | None:
    """Match a job to the saved config that produced it (best effort)."""
    best: str | None = None
    for cfg in configs:
        if cfg.pipeline != job.pipeline:
            continue
        if cfg.input_address == job.input_address and cfg.output_address == job.output_address:
            return str(cfg.name)
        if best is None:
            best = str(cfg.name)
    return best


def _run_paused(instance_id: str) -> bool:
    from ovid2.config import run_control

    return run_control.is_paused(instance_id)


def live_state(*, recent_minutes: int = 30) -> dict[str, Any]:
    """Return the live state of the workspace for the Atlas animation.

    One poll for every running / queued job (and the ones that ended in
    the last ``recent_minutes``, so a finishing beam fades out instead of
    vanishing). Per job: status, pause, the matched config node, and the
    per-table and per-stage timings saved by :mod:`run_items_svc` (folded
    fresh from the event log while live). Timers are server times; the
    browser corrects for clock skew with ``server_now``.

    Returns:
        ``{server_now, jobs: [...], workflow_runs: [...]}``.
    """
    from ovid2.web.services import jobs_svc, run_items_svc

    configs = _configs()
    now = datetime.now(UTC)
    jobs: list[Any] = []
    for status in _LIVE_JOB:
        jobs.extend(jobs_svc.list_jobs(limit=50, status=status).jobs)
    for job in jobs_svc.list_jobs(limit=20).jobs:
        if job.status in _LIVE_JOB or not job.ended_at:
            continue
        try:
            ended = datetime.fromisoformat(job.ended_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        if (now - ended).total_seconds() <= recent_minutes * 60 and job not in jobs:
            jobs.append(job)
    seen: set[str] = set()
    out_jobs: list[dict[str, Any]] = []
    for job in jobs:
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        brief = job_brief(job)
        brief["config"] = _config_for_job(job, configs)
        brief["kind"] = str(job.extra_args.get("kind") or "etl")
        brief["engine"] = str(job.extra_args.get("engine") or "pyspark")
        brief["pausable"] = jobs_svc.is_pausable(job)
        brief["tables"] = list(job.tables)
        brief["workflow_run_id"] = job.workflow_run_id
        items: list[dict[str, Any]] = []
        paused = False
        if job.instance_id:
            live = job.status in _LIVE_JOB
            items = run_items_svc.times_for_page(
                job.instance_id, live=live, job_status=job.status, ended_at=job.ended_at
            )
            paused = live and _run_paused(job.instance_id)
        brief["paused"] = paused
        brief["items"] = [i for i in items if i.get("kind") in ("table", "check")]
        brief["stages"] = [i for i in items if i.get("kind") == run_items_svc.ITEM_STAGE]
        out_jobs.append(brief)
    out_jobs.sort(key=lambda j: (j["status"] not in _LIVE_JOB, j["submitted_at"]), reverse=False)
    return {
        "server_now": now.isoformat(),
        "server_now_ms": int(now.timestamp() * 1000),
        "jobs": out_jobs,
        "workflow_runs": _live_workflow_runs(),
    }


def _live_workflow_runs() -> list[dict[str, Any]]:
    from ovid2.web.services import workflow_runner_svc

    page = _safe(lambda: workflow_runner_svc.list_runs(limit=20), None)
    if page is None:
        return []
    out: list[dict[str, Any]] = []
    for run in page.runs:
        if run.status not in workflow_runner_svc.ACTIVE_RUN_STATUSES:
            continue
        out.append(
            {
                "run_id": run.run_id,
                "workflow": run.workflow,
                "status": run.status,
                "started_at": run.started_at,
                "steps": [
                    {
                        "id": s.step_id,
                        "pipeline": s.pipeline,
                        "status": s.status,
                        "job_id": s.job_id,
                    }
                    for s in run.steps
                ],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------


def heat(*, days: int = 30) -> dict[str, Any]:
    """Return per-config / per-table historical timing and failure rates.

    Aggregated from ``web_jobs`` and ``web_run_items`` of the active
    workspace over the last ``days`` days. Numbers only.

    Returns:
        ``{configs: {name: {runs, failed, median_ms, last_ms}}, tables:
        {pipeline: {table: {runs, failed, median_ms}}}, stages: {pipeline:
        {stage: median_ms}}}``.
    """
    from ovid2.web.services import jobs_svc

    configs = _configs()
    since = datetime.now(UTC).timestamp() - days * 86400
    jobs = [
        j
        for j in jobs_svc.list_jobs(limit=500).jobs
        if j.started_at
        and _ts(j.started_at) >= since
        and j.status in ("done", "failed", "cancelled")
    ]
    per_cfg: dict[str, list[tuple[float, bool]]] = {}
    inst_pipeline: dict[str, str] = {}
    for job in jobs:
        name = _config_for_job(job, configs)
        if not name or not job.ended_at:
            continue
        ms = max(0.0, (_ts(job.ended_at) - _ts(job.started_at or job.ended_at)) * 1000.0)
        per_cfg.setdefault(name, []).append((ms, job.status != "done"))
        if job.instance_id:
            inst_pipeline[job.instance_id] = job.pipeline
    out_cfg = {
        name: {
            "runs": len(vals),
            "failed": sum(1 for _, f in vals if f),
            "median_ms": _median([v for v, _ in vals]),
            "last_ms": vals[0][0],
        }
        for name, vals in per_cfg.items()
    }
    tables: dict[str, dict[str, list[tuple[float, bool]]]] = {}
    stages: dict[str, dict[str, list[float]]] = {}
    if inst_pipeline:
        with web_db.connect() as conn:
            marks = ",".join("?" for _ in inst_pipeline)
            rows = conn.execute(
                "SELECT instance_id, item_kind, item, status, elapsed_ms FROM web_run_items "
                "WHERE workspace_id = ? AND instance_id IN (" + marks + ")",
                (active_workspace(), *inst_pipeline),
            ).fetchall()
        for r in rows:
            pipeline = inst_pipeline.get(str(r["instance_id"]), "")
            ms = float(r["elapsed_ms"] or 0)
            if r["item_kind"] == "table":
                tables.setdefault(pipeline, {}).setdefault(str(r["item"]), []).append(
                    (ms, str(r["status"]) == "failed")
                )
            elif r["item_kind"] == "stage":
                stage = str(r["item"]).rpartition("/")[2]
                stages.setdefault(pipeline, {}).setdefault(stage, []).append(ms)
    return {
        "days": days,
        "configs": out_cfg,
        "tables": {
            p: {
                t: {
                    "runs": len(v),
                    "failed": sum(1 for _, f in v if f),
                    "median_ms": _median([x for x, _ in v]),
                }
                for t, v in per.items()
            }
            for p, per in tables.items()
        },
        "stages": {p: {s: _median(v) for s, v in per.items()} for p, per in stages.items()},
    }


def _ts(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def health_overlay() -> dict[str, Any]:
    """Return the latest data-health result per output address and table.

    Reads the newest ``health`` report source of each pipeline from the
    Report Studio catalogue (aggregates only — never ``value_str`` /
    row values) and maps every failed check to the tables its metrics
    point at, with the remediation fixers / transforms from the check
    catalogue.

    Returns:
        ``{addresses: {address: {source_id, generated_at, summary,
        tables: {table: [{check_id, name, status, severity, exceptions,
        remediation}]}}}}``.
    """
    from ovid2.web.services import health_report_svc, report_sources_svc

    page = _safe(lambda: report_sources_svc.list_sources(kind="health", limit=200), {"sources": []})
    catalogue = _safe(health_report_svc.check_catalogue, {})
    out: dict[str, dict[str, Any]] = {}
    for row in page.get("sources", []):
        doc = _safe(lambda r=row: report_sources_svc.get_source(r["source_id"]), None)
        if not doc:
            continue
        prov = doc.get("provenance") or {}
        address = str(prov.get("input_address") or prov.get("output_address") or "")
        if not address or address in out:
            continue  # newest first: keep the first per address
        checks = ((doc.get("datasets") or {}).get("checks") or {}).get("rows") or []
        metrics = ((doc.get("datasets") or {}).get("exceptions") or {}).get("rows") or []
        by_check_tables: dict[str, set[str]] = {}
        for m in metrics:
            by_check_tables.setdefault(str(m.get("check_id")), set()).add(
                str(m.get("source_table") or "").lower()
            )
        tables: dict[str, list[dict[str, Any]]] = {}
        for c in checks:
            if c.get("status") not in ("exception", "error"):
                continue
            cid = str(c.get("check_id"))
            meta = catalogue.get(cid) or {}
            entry = {
                "check_id": cid,
                "name": c.get("check_name"),
                "status": c.get("status"),
                "severity": c.get("severity") or meta.get("severity") or "warning",
                "exceptions": int(c.get("exception_count") or 0),
                "remediation": meta.get("remediation") or {},
            }
            targets = by_check_tables.get(cid) or {str(c.get("edc_table") or "").lower() or "*"}
            for t in targets:
                tables.setdefault(t or "*", []).append(entry)
        summary = ((doc.get("datasets") or {}).get("summary") or {}).get("rows") or [{}]
        out[address] = {
            "source_id": doc.get("source_id"),
            "instance_id": doc.get("instance_id"),
            "pipeline": doc.get("pipeline"),
            "generated_at": doc.get("generated_at"),
            "summary": summary[0] if summary else {},
            "tables": tables,
        }
    return {"addresses": out}


def _fingerprint_package(pipeline: str) -> tuple[str, float]:
    """Return ``(sha, newest mtime)`` of a package's declarative specs."""
    from ovid2.web.services.pipeline_inspect_svc import _package_dir

    pkg = _package_dir(pipeline)
    if pkg is None:
        return "", 0.0
    h = hashlib.sha256()
    newest = 0.0
    for path in sorted(pkg.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix not in (".yaml", ".yml", ".py", ".json"):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        h.update(str(path.relative_to(pkg)).encode())
        h.update(str(stat.st_size).encode())
        h.update(str(int(stat.st_mtime)).encode())
    return h.hexdigest()[:16], newest


def _mark_changed(changed: list[dict[str, Any]], since: float, nid: str, path: Path | None) -> None:
    """Append ``nid`` to ``changed`` when ``path`` was modified after ``since``."""
    if path is None:
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if mtime > since:
        changed.append({"node": nid, "at": datetime.fromtimestamp(mtime, UTC).isoformat()})


def config_drift() -> dict[str, Any]:
    """Return the nodes whose definition changed since their last successful run.

    A config is *drifted* when its JSON file, its package's specs, its
    engine config or one of its addresses was modified after the last
    ``done`` job of that config started. Metadata only (file mtimes).

    Returns:
        ``{configs: {name: {last_success_at, changed: [{node, at}]}}}``.
    """
    from ovid2.config import connections as _conn
    from ovid2.config import engines as _eng
    from ovid2.web.services import addresses_svc, jobs_svc, pipelines_svc

    configs = _configs()
    done = jobs_svc.list_jobs(limit=500, status="done").jobs
    out: dict[str, Any] = {}
    for cfg in configs:
        last = next(
            (
                j
                for j in done
                if j.pipeline == cfg.pipeline
                and j.input_address == cfg.input_address
                and j.output_address == cfg.output_address
            ),
            None,
        )
        if last is None or not last.started_at:
            out[cfg.name] = {"last_success_at": None, "changed": [], "never_succeeded": True}
            continue
        since = _ts(last.started_at)
        changed: list[dict[str, Any]] = []
        detail = pipelines_svc.get_pipeline(cfg.name)
        _mark_changed(
            changed, since, node_id("config", cfg.name), Path(detail.path) if detail else None
        )
        _, pkg_mtime = _fingerprint_package(cfg.pipeline)
        if pkg_mtime > since:
            changed.append(
                {
                    "node": node_id("package", cfg.pipeline),
                    "at": datetime.fromtimestamp(pkg_mtime, UTC).isoformat(),
                }
            )
        engine_path = _safe(lambda e=cfg.engine: _eng.locate_engine(e), None)
        _mark_changed(changed, since, node_id("engine", cfg.engine), engine_path)
        for name in (cfg.input_address, cfg.output_address):
            if not name:
                continue
            addr_path = _safe(lambda n=name: addresses_svc._locate(n), None)
            _mark_changed(changed, since, node_id("address", name), addr_path)
            addr = _safe(lambda n=name: addresses_svc.get_address(n), None)
            if addr is not None and addr.connection:
                conn_path = _safe(lambda c=addr.connection: _conn.locate_connection(c), None)
                _mark_changed(changed, since, node_id("account", addr.connection), conn_path)
        out[cfg.name] = {"last_success_at": last.started_at, "changed": changed}
    return {"configs": out}


def impact(nid: str) -> dict[str, Any]:
    """Return everything downstream of node ``nid`` (the blast radius).

    Follows the static graph forwards: an account reaches its addresses;
    an address the configs reading it; a config its output address and,
    transitively, every config reading that output (and workflow ``flows``
    links); a package / engine the configs using it. Field-level impact
    inside a package comes from the lineage (``field`` → every step that
    reads it and every field those steps write).

    Returns:
        ``{root, nodes: [ids], edges: [ids], fields?: {...}}``.
    """
    kind, _, rest = nid.partition(":")
    graph = build_graph()
    out_edges: dict[str, list[dict[str, Any]]] = {}
    for e in graph["edges"]:
        out_edges.setdefault(e["source"], []).append(e)
    forward = {"holds", "reads", "writes", "runs_on", "implements", "flows", "step"}
    reached: set[str] = set()
    edge_ids: set[str] = set()
    frontier = [nid]
    # ``runs_on`` points config → engine; impact of an engine flows to the
    # configs using it, so walk that edge kind backwards.
    by_target: dict[str, list[dict[str, Any]]] = {}
    for e in graph["edges"]:
        by_target.setdefault(e["target"], []).append(e)
    while frontier:
        cur = frontier.pop()
        if cur in reached:
            continue
        reached.add(cur)
        nexts = [
            e for e in out_edges.get(cur, []) if e["kind"] in forward and e["kind"] != "runs_on"
        ]
        nexts += [e for e in by_target.get(cur, []) if e["kind"] == "runs_on"]
        for e in nexts:
            other = e["target"] if e["source"] == cur else e["source"]
            edge_ids.add(e["id"])
            frontier.append(other)
    result: dict[str, Any] = {"root": nid, "nodes": sorted(reached), "edges": sorted(edge_ids)}
    if kind == "field":
        pipeline, _, tail = rest.partition("/")
        table, _, field = tail.partition("/")
        result["fields"] = field_impact(pipeline, table, field)
    return result


def field_impact(pipeline: str, table: str, field: str) -> dict[str, Any]:
    """Return the steps and fields downstream of ``field`` inside one table."""
    from ovid2.web.services import pipeline_lineage_svc

    lineage = pipeline_lineage_svc.table_lineage(pipeline, table).to_dict()
    steps = lineage.get("steps", [])
    touched_fields = {field}
    touched_steps: list[str] = []
    grew = True
    while grew:  # each step joins once, so this terminates
        grew = False
        for step in steps:
            if step["id"] in touched_steps:
                continue
            if touched_fields & set(step.get("reads") or []):
                touched_steps.append(step["id"])
                touched_fields |= set(step.get("writes") or [])
                grew = True
    return {
        "pipeline": pipeline,
        "table": table,
        "field": field,
        "steps": touched_steps,
        "fields": sorted(touched_fields - {field}),
    }


# ---------------------------------------------------------------------------
# Drag-to-rebind check
# ---------------------------------------------------------------------------


def check_rebind(config: str, *, role: str, target: str) -> dict[str, Any]:
    """Check whether ``config`` can be re-bound to ``target`` before saving.

    ``role`` is ``engine`` (target = engine config / type name),
    ``input`` or ``output`` (target = address name). Runs the same static
    checks submit does: the capability plan for an engine, read-only
    output storage for an output, and the engine ↔ storage compatibility
    pre-flight.

    Returns:
        ``{ok, reason, missing?, patch}`` where ``patch`` is the
        ``PipelineWrite`` field change the browser saves on confirm.

    Raises:
        LookupError: Unknown config.
        ValueError: Unknown role.
    """
    from ovid2.web.services import pipelines_svc

    detail = pipelines_svc.get_pipeline(config)
    if detail is None:
        raise LookupError(f"Pipeline '{config}' not found.")
    if role not in ("engine", "input", "output"):
        raise ValueError("role must be engine, input or output")
    engine = target if role == "engine" else detail.engine
    input_address = target if role == "input" else detail.input_address
    output_address = target if role == "output" else detail.output_address
    field = {"engine": "engine", "input": "input_address", "output": "output_address"}[role]
    result: dict[str, Any] = {"ok": True, "reason": "", "patch": {field: target}}
    from ovid2.config.engines import resolve_engine_type

    engine_type = resolve_engine_type(engine)
    if role == "engine" and engine_type is None:
        return {**result, "ok": False, "reason": f"'{engine}' is not an engine."}
    if role in ("input", "output"):
        from ovid2.web.services import addresses_svc

        if addresses_svc.get_address(target) is None:
            return {**result, "ok": False, "reason": f"Address '{target}' not found."}
    try:
        from ovid2.drivers.engine_compat import assert_engine_type_supports_drivers

        assert_engine_type_supports_drivers(
            engine or "pyspark",
            [a for a in (input_address, output_address) if a],
            output_names=[output_address] if output_address else [],
        )
    except RuntimeError as exc:
        return {**result, "ok": False, "reason": str(exc)}
    from ovid2.web.kinds import get_kind

    spec = get_kind(detail.kind)
    if spec is not None and spec.package_required and engine_type:
        from ovid2.config.capability_plan import CapabilityPlanError, check_pipeline

        try:
            check_pipeline(
                engine_type, detail.pipeline, list(detail.tables), list(detail.transformations)
            )
        except CapabilityPlanError as exc:
            return {
                **result,
                "ok": False,
                "reason": str(exc),
                "missing": getattr(exc, "missing", {}),
            }
    return result


# ---------------------------------------------------------------------------
# Steward context (read-only, row-free)
# ---------------------------------------------------------------------------


def subgraph(ids: list[str], *, depth: int = 1) -> dict[str, Any]:
    """Return the selected nodes plus their neighbours, row-free.

    The assistant's ``atlas_subgraph`` tool reads this: node metadata and
    links only — no secret values, no row values, no file paths.

    Args:
        ids: Selected node ids.
        depth: Neighbour hops to include (0-2).
    """
    depth = max(0, min(int(depth), 2))
    graph = build_graph()
    keep = set(ids)
    for _ in range(depth):
        more = set()
        for e in graph["edges"]:
            if e["source"] in keep or e["target"] in keep:
                more.add(e["source"])
                more.add(e["target"])
        keep |= more
    return {
        "workspace": graph["workspace"],
        "selected": [i for i in ids if any(n["id"] == i for n in graph["nodes"])],
        "nodes": [n for n in graph["nodes"] if n["id"] in keep],
        "edges": [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep],
    }


__all__ = [
    "EDGE_KINDS",
    "LAYERS",
    "NODE_KINDS",
    "STAGES",
    "build_graph",
    "check_rebind",
    "config_drift",
    "field_impact",
    "health_overlay",
    "heat",
    "impact",
    "job_brief",
    "live_state",
    "node_detail",
    "node_id",
    "subgraph",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.web.services.atlas_svc is a library module.")
