"""Unit tests — the Ovid Atlas (/navigator): graph, live, overlays, pause, state.

Covers :mod:`ovid2.web.services.atlas_svc`,
:mod:`ovid2.web.services.atlas_state_svc`, the ``/api/v1/atlas`` routes,
pause / resume of jobs (``jobs_svc`` + worker flag sync + the executor
gate), the per-table stage events and their fold in ``run_items_svc``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_IID = "20261126-120000-abc123"


def _cfg_dir(kind: str) -> Path:
    d = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed(tmp: Path) -> None:
    """One disc storage account, two locations, an engine and a run config."""
    root = tmp / "data"
    (root / "in").mkdir(parents=True)
    (root / "out").mkdir(parents=True)
    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "lake.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(root), "mount_mode": "rw"}}),
        encoding="utf-8",
    )
    cfgs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (cfgs / "lake_in.json").write_text(
        json.dumps({"connection": "lake", "location": {"path": "in"}}), encoding="utf-8"
    )
    (cfgs / "lake_out.json").write_text(
        json.dumps({"connection": "lake", "location": {"path": "out"}}), encoding="utf-8"
    )
    (Path(os.environ["OVID2_ENGINES_DIR"]) / "duck.json").write_text(
        json.dumps({"type": "duckdb", "args": {}}), encoding="utf-8"
    )
    (_cfg_dir("pipelines") / "demo.json").write_text(
        json.dumps(
            {
                "kind": "etl",
                "pipeline": "example_pcornet",
                "cdm": "pcornet_7_0",
                "input_address": "lake_in",
                "output_address": "lake_out",
                "engine": "duck",
                "trigger": {"type": "manual"},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def seeded(isolated_xdg: Path) -> Path:
    from ovid2.config.pipeline_packages import ensure_pipeline_packages_on_path

    ensure_pipeline_packages_on_path()
    _seed(isolated_xdg)
    return isolated_xdg


# ---------------------------------------------------------------- graph


def test_graph_has_three_layers_and_a_beam(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    g = atlas_svc.build_graph()
    ids = {n["id"]: n for n in g["nodes"]}
    assert g["layers"] == ["storage", "compute", "algorithm"]
    assert ids["config:demo"]["layer"] == "algorithm"
    assert ids["engine:duck"]["layer"] == "compute"
    assert ids["engine:duck"]["meta"]["type"] == "duckdb"
    assert ids["address:lake_in"]["layer"] == "storage"
    assert ids["address:lake_in"]["parent"] == "account:lake"
    assert ids["address:lake_in"]["meta"]["dataspace_path"] == "/lake/in"
    assert ids["package:example_pcornet"]["layer"] == "algorithm"
    edges = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    # The beam: input → config → output, config → engine, package → config.
    assert ("reads", "address:lake_in", "config:demo") in edges
    assert ("writes", "config:demo", "address:lake_out") in edges
    assert ("runs_on", "config:demo", "engine:duck") in edges
    assert ("implements", "package:example_pcornet", "config:demo") in edges
    assert ("holds", "account:lake", "address:lake_in") in edges
    assert re.fullmatch(r"[0-9a-f]{16}", g["digest"])
    # Static and secret-free: no file paths or secret refs leak into nodes.
    blob = json.dumps(g)
    assert str(seeded) not in blob
    assert "secret" not in blob.lower()


def test_workflow_node_carries_its_dag(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workflow node lists its steps (with overrides) and step-id links."""
    from ovid2.web.models import WorkflowWrite
    from ovid2.web.services import atlas_svc, workflows_svc

    monkeypatch.setenv("OVID2_WORKFLOWS_DIR", str(_cfg_dir("workflows")))
    wf = WorkflowWrite.model_validate(
        {
            "steps": [
                {"id": "a", "pipeline": "demo", "overrides": {"output_address": "lake_in"}},
                {"id": "b", "pipeline": "demo", "join": "any"},
            ],
            "edges": [{"from": "a", "to": "b", "type": "after", "on": "failure"}],
        }
    )
    workflows_svc.write_workflow("chain", wf, create_only=True)
    g = atlas_svc.build_graph()
    m = next(n for n in g["nodes"] if n["id"] == "workflow:chain")["meta"]
    assert m["edges"] == [{"from": "a", "to": "b", "type": "after", "on": "failure"}]
    assert m["steps"][0]["output_address"] == "lake_in"
    assert m["steps"][0]["input_address"] is None
    assert m["steps"][1]["join"] == "any"


def test_graph_digest_changes_with_the_setup(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    before = atlas_svc.build_graph()["digest"]
    (Path(os.environ["OVID2_ENGINES_DIR"]) / "spark2.json").write_text(
        json.dumps({"type": "pyspark", "args": {}}), encoding="utf-8"
    )
    assert atlas_svc.build_graph()["digest"] != before


def test_node_detail_and_unknown_node(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    d = atlas_svc.node_detail("config:demo")
    assert d["config"]["engine"] == "duck"
    assert atlas_svc.node_detail("address:lake_out")["dataspace_path"] == "/lake/out"
    assert "lineage" in atlas_svc.node_detail("package:example_pcornet")
    with pytest.raises(LookupError):
        atlas_svc.node_detail("config:nope")
    with pytest.raises(LookupError):
        atlas_svc.node_detail("bogus:x")


def test_impact_follows_beams(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    r = atlas_svc.impact("address:lake_in")
    assert "config:demo" in r["nodes"] and "address:lake_out" in r["nodes"]
    r = atlas_svc.impact("engine:duck")
    assert "config:demo" in r["nodes"]
    r = atlas_svc.impact("account:lake")
    assert {"address:lake_in", "config:demo"} <= set(r["nodes"])


def test_field_impact_is_transitive(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    r = atlas_svc.field_impact("example_pcornet", "demographic", "PATID")
    assert r["field"] == "PATID" and isinstance(r["steps"], list)


def test_rebind_check(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    ok = atlas_svc.check_rebind("demo", role="engine", target="pyspark")
    assert ok["ok"] and ok["patch"] == {"engine": "pyspark"}
    bad = atlas_svc.check_rebind("demo", role="engine", target="not-an-engine")
    assert not bad["ok"] and "not an engine" in bad["reason"]
    missing = atlas_svc.check_rebind("demo", role="output", target="nowhere")
    assert not missing["ok"]
    with pytest.raises(ValueError):
        atlas_svc.check_rebind("demo", role="bogus", target="x")
    with pytest.raises(LookupError):
        atlas_svc.check_rebind("nope", role="engine", target="duck")


def test_subgraph_is_row_free(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    sg = atlas_svc.subgraph(["config:demo"], depth=1)
    ids = {n["id"] for n in sg["nodes"]}
    assert {"config:demo", "engine:duck", "address:lake_in"} <= ids
    assert sg["selected"] == ["config:demo"]


# ----------------------------------------------------------------- live


def _job(status: str = "running", **kw: Any) -> str:
    from ovid2.web import db as web_db
    from ovid2.workspaces import active_workspace

    web_db.ensure_schema()
    job_id = kw.pop("job_id", "j1")
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, submitted_by, pipeline, batch_id, "
            "input_address, output_address, tables_json, transformations_json, extra_args_json, "
            "status, instance_id, started_at, workspace_id) "
            "VALUES (?, '2026-11-26T12:00:00+00:00', 'web', 'example_pcornet', 'lake/in', "
            "'lake_in', 'lake_out', '[\"demographic\"]', '[\"all\"]', ?, ?, ?, "
            "'2026-11-26T12:00:00+00:00', ?)",
            (
                job_id,
                json.dumps({"kind": kw.pop("kind", "etl"), "engine": "duck"}),
                status,
                kw.pop("instance_id", _IID),
                active_workspace(),
            ),
        )
    return job_id


def _log(events: list[dict[str, Any]], iid: str = _IID) -> None:
    work = Path(os.environ["OVID2_WORK_DIR"])
    work.mkdir(parents=True, exist_ok=True)
    (work / f"events-{iid}.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )


def _enter(table: str, stage: str, ts: str, lazy: bool = True) -> dict[str, Any]:
    return {
        "kind": "stage.progress",
        "table": table,
        "stage": stage,
        "ts": ts,
        "extra": {"step": "enter", "current": 1, "total": 7, "lazy": lazy},
    }


def test_stage_items_fold_per_table() -> None:
    from ovid2.web.services.run_items_svc import ITEM_STAGE, fold_events

    events = [
        {"kind": "table.started", "table": "demographic", "ts": "2026-11-26T12:00:00+00:00"},
        _enter("demographic", "read", "2026-11-26T12:00:00+00:00"),
        _enter("demographic", "format", "2026-11-26T12:00:01+00:00"),
        _enter("demographic", "write", "2026-11-26T12:00:02+00:00", lazy=False),
    ]
    recs = {(r.item_kind, r.item): r for r in fold_events(events)}
    assert recs[(ITEM_STAGE, "demographic/read")].status == "completed"
    assert recs[(ITEM_STAGE, "demographic/read")].elapsed_ms == 1000
    w = recs[(ITEM_STAGE, "demographic/write")]
    assert w.status == "running" and w.display_name == "exec"
    # The table's own clock is never paused by an enter event.
    assert recs[("table", "demographic")].status == "running"
    events.append(
        {"kind": "table.completed", "table": "demographic", "ts": "2026-11-26T12:00:12+00:00"}
    )
    recs = {(r.item_kind, r.item): r for r in fold_events(events)}
    assert recs[(ITEM_STAGE, "demographic/write")].status == "completed"
    assert recs[(ITEM_STAGE, "demographic/write")].elapsed_ms == 10000


def test_live_state_reports_running_job(seeded: Path) -> None:
    from ovid2.web.services import atlas_svc

    _job()
    _log(
        [
            {"kind": "table.started", "table": "demographic", "ts": "2026-11-26T12:00:00+00:00"},
            _enter("demographic", "map", "2026-11-26T12:00:01+00:00"),
        ]
    )
    live = atlas_svc.live_state()
    (job,) = live["jobs"]
    assert job["config"] == "demo" and job["status"] == "running" and job["pausable"]
    assert [i["item"] for i in job["items"]] == ["demographic"]
    assert job["stages"][0]["item"] == "demographic/map"
    assert live["server_now_ms"] > 0


def test_heat_and_drift_are_aggregates(seeded: Path) -> None:
    from ovid2.web import db as web_db
    from ovid2.web.services import atlas_svc

    _job("done")
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_jobs SET ended_at = '2026-11-26T12:01:00+00:00', "
            "started_at = ? WHERE job_id = 'j1'",
            (__import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),),
        )
    h = atlas_svc.heat()
    assert set(h) >= {"configs", "tables", "stages"}
    d = atlas_svc.config_drift()
    assert "demo" in d["configs"]


# ---------------------------------------------------------------- pause


def test_pause_resume_and_worker_flag(seeded: Path) -> None:
    from ovid2.config import run_control
    from ovid2.web.services import jobs_svc
    from ovid2.web.worker import Worker as WebWorker

    _job()
    assert jobs_svc.pause("j1").pause_requested is True
    WebWorker.__new__(WebWorker)._sync_pause_flags()
    assert run_control.is_paused(_IID)
    assert jobs_svc.resume("j1").pause_requested is False
    WebWorker.__new__(WebWorker)._sync_pause_flags()
    assert not run_control.is_paused(_IID)


def test_pause_refuses_terminal_and_one_shot_jobs(seeded: Path) -> None:
    from ovid2.web.services import jobs_svc

    _job("done", job_id="j2", instance_id="20261126-120000-abc124")
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.pause("j2")
    _job("running", job_id="j3", instance_id="20261126-120000-abc125", kind="export")
    with pytest.raises(jobs_svc.SubmitError, match="cannot pause"):
        jobs_svc.pause("j3")
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.pause("missing")


def test_pause_flag_rejects_free_text() -> None:
    from ovid2.config import run_control

    with pytest.raises(ValueError):
        run_control.pause_flag_path("../../etc/passwd")
    assert run_control.is_paused("nope") is False


def test_executor_gate_holds_until_resumed(isolated_xdg: Path) -> None:
    from ovid2.config import run_control
    from ovid2.core import cancellation
    from ovid2.core.pause import wait_if_paused

    cancellation.reset()
    assert wait_if_paused(_IID) is False
    run_control.request_pause(_IID)
    seen: list[str] = []
    sleeps = {"n": 0}

    def _sleep(_s: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 3:
            run_control.clear_pause(_IID)

    held = wait_if_paused(
        _IID,
        on_pause=lambda: seen.append("pause"),
        on_resume=lambda: seen.append("resume"),
        sleep=_sleep,
    )
    assert held and seen == ["pause", "resume"] and sleeps["n"] == 3


def test_cancel_while_paused_unwinds(isolated_xdg: Path) -> None:
    from ovid2.config import run_control
    from ovid2.core import cancellation
    from ovid2.core.pause import wait_if_paused

    cancellation.reset()
    run_control.request_pause(_IID)
    try:
        with pytest.raises(KeyboardInterrupt):
            wait_if_paused(_IID, sleep=lambda _s: cancellation.request_cancel())
    finally:
        cancellation.reset()
        run_control.clear_pause(_IID)


def test_run_tables_calls_the_boundary_hook_before_every_table() -> None:
    from ovid2.core.table_runner import run_tables

    order: list[str] = []
    run_tables(
        ["a", "b"],
        lambda t: order.append("run:" + t),
        before_table=lambda: order.append("gate"),
    )
    assert order == ["gate", "run:a", "gate", "run:b"]


def test_pipeline_emits_stage_enter_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from ovid2.core import pipeline as pipeline_mod
    from ovid2.transformations.base import BaseTransformer

    fake_cdm = MagicMock()
    fake_cdm.reference_tables = set()
    monkeypatch.setattr(pipeline_mod, "resolve_cdm_for_pipeline", lambda p: fake_cdm)
    monkeypatch.setattr(pipeline_mod, "_register_auxiliary_tables", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod, "_read_with_pipeline_hook", lambda **k: MagicMock())
    monkeypatch.setattr(pipeline_mod, "_resolve_endpoint", lambda e, a, *x, **k: a)

    class _Fmt:
        def format(self, df: object) -> object:
            return df

    class _Map(BaseTransformer):
        stage_name = "map"

        def transform(self, df: Any, context: dict[str, Any]) -> Any:
            return df

    emitted: list[Any] = []
    bus = MagicMock()
    bus.emit.side_effect = emitted.append
    pipeline_mod.run_pipeline(
        engine=MagicMock(),
        input_address=MagicMock(),
        output_address=MagicMock(),
        table_name="demographic",
        formatter=_Fmt(),
        transformations=[_Map()],
        pipeline="example_pcornet",
        batch_id="b",
        event_bus=bus,
        instance_id=_IID,
    )
    stages = [
        (e.payload["stage"], e.payload["extra"]["lazy"])
        for e in emitted
        if e.kind == "stage.progress"
    ]
    assert stages == [("read", True), ("format", True), ("map", True), ("write", False)]
    assert emitted[0].kind == "table.started"
    assert [e.kind for e in emitted][-1] == "table.completed"


# ----------------------------------------------------------- views/presence


def test_layouts_are_shared_and_owner_deletable(isolated_xdg: Path) -> None:
    from ovid2.web.services import atlas_state_svc as s

    s.save_layout("ops", {"layers": ["storage"], "mode": "flat"}, actor="a@example.org")
    assert [v["name"] for v in s.list_layouts()] == ["ops"]
    with pytest.raises(s.AtlasStateError):
        s.save_layout("x", {"bogus": 1}, actor="a@example.org")
    with pytest.raises(s.AtlasStateError):
        s.save_layout("../bad", {}, actor="a@example.org")
    with pytest.raises(PermissionError):
        s.delete_layout("ops", actor="b@example.org")
    assert s.delete_layout("ops", actor="a@example.org") is True
    assert s.delete_layout("ops", actor="a@example.org") is False


def test_layouts_are_workspace_scoped(isolated_xdg: Path) -> None:
    from ovid2.web.services import atlas_state_svc as s
    from ovid2.workspaces import use_workspace

    s.save_layout("ops", {"mode": "flat"}, actor="a")
    with use_workspace("other"):
        assert s.list_layouts() == []


def test_presence_heartbeat_and_soft_lock(isolated_xdg: Path) -> None:
    from ovid2.web.services import atlas_state_svc as s

    s.heartbeat(
        user_id="u1", display_name="a@x", client_id="tab-aaaaaa", node="config:demo", mode="edit"
    )
    others = s.heartbeat(
        user_id="u2", display_name="b@x", client_id="tab-bbbbbb", node=None, mode="view"
    )
    assert [p["display_name"] for p in others] == ["a@x"]
    assert s.locks_for("config:demo", exclude_user="u2")[0]["mode"] == "edit"
    assert s.locks_for("config:demo", exclude_user="u1") == []
    with pytest.raises(s.AtlasStateError):
        s.heartbeat(user_id="u", display_name="", client_id="x", node=None, mode="view")
    with pytest.raises(s.AtlasStateError):
        s.heartbeat(user_id="u", display_name="", client_id="tab-cccccc", node=None, mode="root")
    s.leave("tab-aaaaaa")
    assert s.locks_for("config:demo") == []


# ------------------------------------------------------------- the routes


def test_page_embeds_graph_and_old_links_work(web_client, seeded: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/navigator?p=example_pcornet&t=demographic&f=SEX&group_by=profile")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="atlas"' in body and "/static/atlas/atlas.js" in body
    assert 'data-initial-p="example_pcornet"' in body and 'data-initial-t="demographic"' in body
    boot = json.loads(re.search(r'id="atlas-boot">(.*?)</script>', body, re.S).group(1))
    assert any(n["id"] == "config:demo" for n in boot["graph"]["nodes"])
    assert 'data-nav="navigator"' in body
    assert r.headers["cache-control"] == "no-store"
    # The lane board: no free-canvas camera, a "running on top" toggle.
    assert "data-atlas-lanes" in body and "data-atlas-minimap" not in body
    assert re.search(r'<input type="checkbox" data-atlas-float-running checked>', body)


def test_api_routes(web_client, seeded: Path) -> None:  # type: ignore[no-untyped-def]
    g = web_client.get("/api/v1/atlas").json()
    assert any(n["id"] == "engine:duck" for n in g["nodes"])
    assert web_client.get("/api/v1/atlas/nodes/config:demo").status_code == 200
    assert web_client.get("/api/v1/atlas/nodes/config:nope").status_code == 404
    assert web_client.get("/api/v1/atlas/live").json()["jobs"] == []
    for path in ("heat", "health", "drift"):
        assert web_client.get("/api/v1/atlas/overlays/" + path).status_code == 200
    assert "config:demo" in web_client.get("/api/v1/atlas/impact?node=engine:duck").json()["nodes"]
    r = web_client.post(
        "/api/v1/atlas/rebind:check", json={"config": "demo", "role": "engine", "target": "pyspark"}
    )
    assert r.json()["ok"] is True
    form = web_client.get("/api/v1/atlas/forms/location/disc").json()
    assert any(f["name"] == "format" and f.get("widget") == "choices" for f in form["args"])
    assert web_client.get("/api/v1/atlas/forms/nope/disc").status_code == 404
    assert (
        web_client.put("/api/v1/atlas/layouts/ops", json={"layout": {"mode": "flat"}}).status_code
        == 200
    )
    assert web_client.get("/api/v1/atlas/layouts").json()[0]["name"] == "ops"
    assert web_client.delete("/api/v1/atlas/layouts/ops").status_code == 204
    assert web_client.delete("/api/v1/atlas/layouts/ops").status_code == 404
    beat = web_client.post(
        "/api/v1/atlas/presence", json={"client_id": "tab-zzzzzz", "node": "config:demo"}
    )
    assert beat.status_code == 200 and beat.json() == []


def test_pause_routes(web_client, seeded: Path) -> None:  # type: ignore[no-untyped-def]
    _job()
    r = web_client.post("/api/v1/jobs/j1/pause")
    assert r.status_code == 200 and r.json()["pause_requested"] is True
    r = web_client.post("/api/v1/jobs/j1/resume")
    assert r.status_code == 200 and r.json()["pause_requested"] is False
    assert web_client.post("/api/v1/jobs/nope/pause").status_code == 400


def test_replay_route(web_client, seeded: Path) -> None:  # type: ignore[no-untyped-def]
    _job("done")
    _log(
        [
            {"kind": "table.started", "table": "demographic", "ts": "2026-11-26T12:00:00+00:00"},
            {"kind": "table.completed", "table": "demographic", "ts": "2026-11-26T12:00:05+00:00"},
        ]
    )
    r = web_client.get(f"/api/v1/atlas/replay/{_IID}")
    assert r.status_code == 200
    assert r.json()["items"][0]["elapsed_ms"] == 5000
    assert web_client.get("/api/v1/atlas/replay/20261126-000000-000000").status_code == 404


def test_viewer_cannot_save_views_or_pause(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Role caps: viewers read the Atlas but never write views / pause jobs."""
    from ovid2.web.auth.roles import ROLE_CAPABILITIES, Capability, Role

    assert Capability.MANAGE_CONFIG not in ROLE_CAPABILITIES[Role.VIEWER]
    assert Capability.SUBMIT_JOBS not in ROLE_CAPABILITIES[Role.VIEWER]


# ------------------------------------------------------- canvas layout (JS)

_STATIC = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static"

_LAYOUT_PROBE = r"""
import { buildLanes, orderLanes, laneConfigs, fitLanes, visibleColumns, groupLanes, dagLayout, LANE_MIN, LANE_GAP, GROUP_GAP, GROUP_HEAD } from "__LAYOUT__";
const nodes = [], edges = [];
const add = (kind, name, layer, meta = {}) => nodes.push({ id: kind + ":" + name, kind, name, layer, meta });
add("engine", "spark", "compute"); add("engine", "duck", "compute"); add("engine", "idle", "compute");
add("account", "disc", "storage");
for (let i = 0; i < 24; i++) add("package", "p" + i, "algorithm");
for (let w = 0; w < 3; w++) add("workflow", "wf" + w, "algorithm", {
  steps: [0, 1, 2, 3, 4, 5].map((k) => ({ id: "s" + k, pipeline: "c" + (w + 3 * (5 - k)) })),
});
for (let i = 0; i < 24; i++) {
  const c = "config:c" + i;
  add("config", "c" + i, "algorithm", { pipeline: "p" + (i % 4) });
  add("address", "in" + i, "storage"); add("address", "out" + i, "storage");
  edges.push({ kind: "implements", source: "package:p" + (i % 4), target: c });
  edges.push({ kind: "runs_on", source: c, target: i % 2 ? "engine:spark" : "engine:duck" });
  edges.push({ kind: "reads", source: "address:in" + i, target: c });
  edges.push({ kind: "writes", source: c, target: "address:out" + i });
  edges.push({ kind: "holds", source: "account:disc", target: "address:in" + i });
  if (i < 18) edges.push({ kind: "step", source: "workflow:wf" + (i % 3), target: c });
}
const g = { nodes, edges };
const all = new Set(["algorithm", "compute", "storage"]);
const lanes = buildLanes(g, { layers: all });
const live = new Set(["config:c20", "config:c4"]); // c4 is a step of wf1
const isLive = (l) => laneConfigs(l).some((c) => live.has(c));
const floated = orderLanes(lanes, { floatRunning: true, isLive });
const still = orderLanes(lanes, { floatRunning: false, isLive });
const fitBig = fitLanes(lanes.length, 2000);
const fitSmall = fitLanes(lanes.length, 300);
const groups = groupLanes(floated);
const fitGrouped = fitLanes(lanes.map((l) => l.weight), 2000, { groups: groups.length });
// A workflow DAG: a → (b, c) → d (join), e after d on failure, f unlinked.
const dag = dagLayout(["a", "b", "c", "d", "e", "f"], [
  { from: "a", to: "b" }, { from: "a", to: "c" }, { from: "b", to: "d" }, { from: "c", to: "d" }, { from: "d", to: "e" },
]);
const chain = dagLayout(["x", "y", "z"], []);
const cyc = dagLayout(["p", "q"], [{ from: "p", to: "q" }, { from: "q", to: "p" }]);
// Step overrides and data links pick a step's real input / output.
add("address", "ovr", "storage");
const g2 = { nodes: nodes.concat([{ id: "workflow:wfx", kind: "workflow", name: "wfx", layer: "algorithm", meta: {
  steps: [{ id: "one", pipeline: "c18", output_address: "ovr" }, { id: "two", pipeline: "c19" }, { id: "again", pipeline: "c18" }],
  edges: [{ from: "one", to: "two", type: "data", on: "success" }, { from: "two", to: "again", type: "after", on: "failure" }],
} }]), edges };
const wfx = buildLanes(g2, { layers: all }).find((l) => l.id === "lane:workflow:wfx");
console.log(JSON.stringify({
  ids: lanes.map((l) => l.id),
  kinds: lanes.map((l) => l.kind),
  wf0Steps: lanes[0].steps.map((s) => s.config),
  wf0Weight: lanes[0].weight,
  groupKeys: groups.map((x) => x.key), groupSizes: groups.map((x) => x.lanes.length),
  floatedInGroup: floated.slice(0, 4).map((l) => l.id),
  fitGrouped, gap: LANE_GAP, groupGap: GROUP_GAP, groupHead: GROUP_HEAD, nLanes: lanes.length,
  totalWeight: lanes.reduce((a, l) => a + l.weight, 0),
  dag: dag.pos, dagCols: dag.cols, dagRows: dag.rows, chain: chain.pos, chainRows: chain.rows, cyc: cyc.pos,
  wfx: { steps: wfx.steps.map((s) => [s.id, s.config]), beams: wfx.beams, edges: wfx.edges, rows: wfx.dag.rows, configs: laneConfigs(wfx) },
  c20: lanes.find((l) => l.id === "lane:config:c20").beam,
  unused: lanes.find((l) => l.kind === "unused").nodes,
  floated: floated.slice(0, 3).map((l) => l.id),
  floatedRest: floated.slice(2).map((l) => l.id),
  still: still.map((l) => l.id),
  fitBig, fitSmall, min: LANE_MIN,
  fillsExactly: fitBig.laneH * lanes.length + LANE_GAP * (lanes.length + 1) <= 2000 &&
    (fitBig.laneH + 1) * lanes.length + LANE_GAP * (lanes.length + 1) > 2000,
  colsNoCompute: visibleColumns(new Set(["algorithm", "storage"])).map((c) => c.key),
  noStorage: buildLanes(g, { layers: new Set(["algorithm", "compute"]) }).map((l) => l.kind).filter((k) => k === "accounts").length,
}));
"""


def _js_runtime() -> str | None:
    import shutil

    for name in ("node", "bun"):
        exe = shutil.which(name) or (
            str(Path.home() / ".bun" / "bin" / "bun")
            if name == "bun" and (Path.home() / ".bun" / "bin" / "bun").exists()
            else None
        )
        if exe:
            return exe
    return None


def _run_probe(tmp_path: Path) -> dict[str, Any]:
    import subprocess

    exe = _js_runtime()
    if exe is None:
        pytest.skip("no node / bun to run static/atlas/layout.js")
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        _LAYOUT_PROBE.replace("__LAYOUT__", (_STATIC / "atlas" / "layout.js").as_uri())
    )
    out = subprocess.run([exe, str(probe)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_layout_one_lane_per_workflow_and_pipeline(tmp_path: Path) -> None:
    """Every workflow and every standalone pipeline gets its own line."""
    r = _run_probe(tmp_path)
    # 3 workflows, then the 6 configs no workflow uses, then accounts / unused.
    assert r["ids"][:3] == ["lane:workflow:wf0", "lane:workflow:wf1", "lane:workflow:wf2"]
    assert r["ids"][3:9] == [f"lane:config:c{i}" for i in range(18, 24)]
    assert r["kinds"][9:] == ["accounts", "unused"]
    # Steps keep the workflow's own order, not the alphabetical one.
    assert r["wf0Steps"] == [f"config:c{i}" for i in (15, 12, 9, 6, 3, 0)]
    # A pipeline lane is its beam in fixed columns.
    assert r["c20"] == {
        "input": ["address:in20"],
        "engine": ["engine:duck"],
        "package": ["package:p0"],
        "output": ["address:out20"],
    }
    # Unused packages first, then unused engines.
    assert r["unused"][0] == "package:p10" and r["unused"][-1] == "engine:idle"
    assert r["colsNoCompute"] == ["input", "package", "output"]
    assert r["noStorage"] == 0


def test_layout_running_lanes_float_to_the_top(tmp_path: Path) -> None:
    """'Running on top' lifts live lanes to the top of their own group."""
    r = _run_probe(tmp_path)
    assert r["floatedInGroup"] == [
        "lane:workflow:wf1",
        "lane:workflow:wf0",
        "lane:workflow:wf2",
        "lane:config:c20",
    ]
    assert r["still"] == r["ids"]


def test_layout_groups_keep_workflows_and_pipelines_apart(tmp_path: Path) -> None:
    """Lanes are grouped (workflows · pipelines · accounts · unused) with a
    wider lane gap and an extra gap + header before every group."""
    r = _run_probe(tmp_path)
    assert r["groupKeys"] == ["workflows", "pipelines", "accounts", "unused"]
    assert r["groupSizes"] == [3, 6, 1, 1]
    assert r["gap"] >= 10 and r["groupGap"] > r["gap"]
    # The grouped fit leaves room for the group headers and gaps.
    fixed = r["gap"] * (r["nLanes"] + 4 + 1) + 4 * r["groupHead"] + 3 * r["groupGap"]
    assert r["fitGrouped"]["laneH"] == (2000 - fixed) // r["totalWeight"]


def test_layout_workflow_is_a_dag_of_pipelines(tmp_path: Path) -> None:
    """A workflow lane lays its steps out by their links (longest path)."""
    r = _run_probe(tmp_path)
    d = r["dag"]
    assert [d[k]["col"] for k in "abcdef"] == [0, 1, 1, 2, 3, 0]
    assert {d["b"]["row"], d["c"]["row"]} == {0, 1}
    assert r["dagCols"] == 4 and r["dagRows"] == 2
    # No links: one row in step order; a cycle never hangs.
    assert [r["chain"][k]["col"] for k in "xyz"] == [0, 1, 2] and r["chainRows"] == 1
    assert set(r["cyc"]) == {"p", "q"}
    # Steps are keyed by step id: one config used twice shows twice.
    w = r["wfx"]
    assert w["steps"] == [["one", "config:c18"], ["two", "config:c19"], ["again", "config:c18"]]
    assert w["configs"] == ["config:c18", "config:c19"]
    # Overrides apply, and a data link feeds the upstream output in.
    assert w["beams"]["one"]["output"] == ["address:ovr"]
    assert w["beams"]["two"]["input"] == ["address:ovr"]
    assert w["beams"]["again"]["input"] == ["address:in18"]
    assert w["edges"][1] == {"from": "two", "to": "again", "type": "after", "on": "failure"}
    assert r["wf0Weight"] == 1


def test_layout_lanes_fill_the_height_then_scroll(tmp_path: Path) -> None:
    """Lanes share the canvas height exactly; below one line they scroll."""
    r = _run_probe(tmp_path)
    assert r["fillsExactly"] is True and r["fitBig"]["scroll"] is False
    assert r["fitBig"]["density"] == "roomy"
    assert r["fitSmall"] == {"laneH": r["min"], "scroll": True, "density": "compact"}


def test_atlas_palette_is_readable() -> None:
    """Dim Atlas text meets WCAG AA (4.5:1) on every card / page fill."""
    css = (_STATIC / "ovid2.css").read_text()
    block = css[css.index(".atlas-page {") :]
    tok = dict(re.findall(r"--atl-(dim|fg|bg|card|card-2):\s*(#[0-9a-fA-F]{6})", block[:600]))

    def lum(h: str) -> float:
        c = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a: str, b: str) -> float:
        la, lb = lum(a), lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for fg in ("dim", "fg"):
        for bg in ("bg", "card", "card-2"):
            assert ratio(tok[fg], tok[bg]) >= 7.0, (fg, bg)
    assert ".atl-lane {" in css and ".atl-lane-head" in css
    # The page is exactly the viewport: only the lane list may scroll.
    page = css[css.index("\n.atlas-page {") :]
    page = page[: page.index("}")]
    assert "100dvh" in page and "overflow: hidden" in page
