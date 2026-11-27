"""Saved per-item run timings (``run_items_svc``) and the run-page island.

The run page used to rebuild every table / check clock in the browser by
replaying the event log, timing each item from when its ``*.started``
line was *read*: finished items ticked for a few seconds and running
items reset to ``00:00`` on every open. These tests pin the fix: the
times are folded from the log, saved in ``web_run_items`` (workspace
scoped), and embedded in the page so the clocks start from real times.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_IID = "20261124-120000-abc123"


def _write_log(events: list[dict[str, object]], iid: str = _IID) -> Path:
    work = Path(os.environ["OVID2_WORK_DIR"])
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"events-{iid}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


_EVENTS: list[dict[str, object]] = [
    {"kind": "instance.started", "ts": "2026-11-24T12:00:00+00:00"},
    {"kind": "table.started", "table": "demographic", "ts": "2026-11-24T12:00:01+00:00"},
    {"kind": "engine.metrics", "ts": "2026-11-24T12:00:02+00:00"},
    {
        "kind": "table.completed",
        "table": "demographic",
        "ts": "2026-11-24T12:00:31+00:00",
        "elapsed_ms": 30000.4,
    },
    {"kind": "table.started", "table": "encounter", "ts": "2026-11-24T12:00:31+00:00"},
    {
        "kind": "table.failed",
        "table": "encounter",
        "ts": "2026-11-24T12:00:41+00:00",
        "error": "boom",
    },
    {"kind": "table.started", "table": "vital", "ts": "2026-11-24T12:00:41+00:00"},
    {
        "kind": "check.started",
        "check_id": "c1",
        "check_name": "Check one",
        "ts": "2026-11-24T12:00:42+00:00",
    },
    {
        "kind": "check.completed",
        "check_id": "c1",
        "check_status": "exception",
        "ts": "2026-11-24T12:00:43+00:00",
        "elapsed_ms": 900,
    },
]


def test_fold_events_records_times_and_status() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    recs = {(r.item_kind, r.item): r for r in fold_events(_EVENTS)}
    demo = recs[("table", "demographic")]
    assert demo.status == "completed" and demo.elapsed_ms == 30000
    assert demo.started_at == "2026-11-24T12:00:01+00:00"
    enc = recs[("table", "encounter")]
    # No elapsed_ms on the event: derived from ts, and ended_at kept.
    assert enc.status == "failed" and enc.elapsed_ms == 10000 and enc.error == "boom"
    assert enc.ended_at == "2026-11-24T12:00:41+00:00"
    vital = recs[("table", "vital")]
    assert vital.status == "running" and vital.elapsed_ms is None
    chk = recs[("check", "c1")]
    assert chk.status == "exception" and chk.display_name == "Check one" and chk.elapsed_ms == 900
    assert [r.item for r in fold_events(_EVENTS)] == ["demographic", "encounter", "vital", "c1"]


def test_replayed_start_does_not_reopen_a_finished_item() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    events = [*_EVENTS[:4], _EVENTS[1]]  # demographic started again after completing
    (demo,) = [r for r in fold_events(events) if r.item == "demographic"]
    assert demo.status == "completed" and demo.elapsed_ms == 30000


def test_sync_saves_and_survives_log_loss(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    path = _write_log(_EVENTS)
    assert run_items_svc.sync_from_log(_IID) is not None
    path.unlink()  # tmpfs log gone (reboot)
    saved = {r.item: r for r in run_items_svc.get_items(_IID)}
    assert saved["demographic"].elapsed_ms == 30000
    assert saved["encounter"].status == "failed"
    # A finished run is served from the saved rows.
    rows = run_items_svc.times_for_page(_IID, live=False)
    assert [r["item"] for r in rows] == ["demographic", "encounter", "vital", "c1"]
    assert rows[0]["kind"] == "table" and rows[-1]["kind"] == "check"


def test_saved_rows_are_workspace_scoped(isolated_xdg: Path) -> None:
    from ovid2 import workspaces
    from ovid2.web.services import run_items_svc

    _write_log(_EVENTS)
    run_items_svc.sync_from_log(_IID)
    with workspaces.use_workspace("other"):
        assert run_items_svc.get_items(_IID) == []


def test_live_run_refolds_the_log(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    _write_log(_EVENTS[:2])
    first = run_items_svc.times_for_page(_IID, live=True)
    assert first[0]["status"] == "running"
    _write_log(_EVENTS[:4])
    again = run_items_svc.times_for_page(_IID, live=True)
    assert again[0]["status"] == "completed" and again[0]["elapsed_ms"] == 30000


def test_partial_trailing_line_is_ignored(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    path = _write_log(_EVENTS[:2])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "table.completed", "table": "demographic"')  # mid-flush
    (rec,) = run_items_svc.sync_from_log(_IID) or []
    assert rec.status == "running"


def test_worker_finalize_saves_item_times(isolated_xdg: Path) -> None:
    from ovid2.web import worker
    from ovid2.web.services import run_items_svc

    _write_log(_EVENTS)
    worker._save_run_item_times(_IID)
    assert len(run_items_svc.get_items(_IID)) == 4


def test_run_page_embeds_item_times(web_client: object, isolated_xdg: Path) -> None:
    _write_log(_EVENTS)
    resp = web_client.get(f"/runs/{_IID}", headers={"X-Requested-With": "fetch"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    m = re.search(
        r'<script type="application/json" class="run-item-times">(.*?)</script>',
        resp.text,
        re.S,
    )
    assert m, "run page must embed the saved item times"
    rows = json.loads(m.group(1))
    by_item = {r["item"]: r for r in rows}
    assert by_item["demographic"]["elapsed_ms"] == 30000
    assert by_item["vital"]["status"] == "running"
    assert by_item["vital"]["started_at"] == "2026-11-24T12:00:41+00:00"


def test_run_page_has_no_workers_row(web_client: object, isolated_xdg: Path) -> None:
    _write_log(_EVENTS)
    resp = web_client.get(f"/runs/{_IID}", headers={"X-Requested-With": "fetch"})  # type: ignore[attr-defined]
    assert "<dt>Workers</dt>" not in resp.text


def test_panel_clock_uses_server_time_not_read_time() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static" / "display"
    shared = (root / "run" / "panel.js").read_text(encoding="utf-8")
    checks = (root / "checks" / "panel.js").read_text(encoding="utf-8")
    for text in (shared, checks):
        assert "performance.now" not in text, "timers must not anchor on the read moment"
    assert "run-item-times" in shared and "seedSavedTimes" in shared
    assert "serverNowMs" in shared and "instance.finished" in shared
    assert "isTerminalItem" in checks


# ---------------------------------------------------------------------------
# Multi-phase (legacy_ovid) runs: per-table stage.progress pause / resume.
# ---------------------------------------------------------------------------


def _stage(table: str, stage: str, state: str, ts: str) -> dict[str, object]:
    return {
        "kind": "stage.progress",
        "table": table,
        "stage": stage,
        "extra": {"state": state},
        "ts": ts,
    }


_LEGACY: list[dict[str, object]] = [
    {"kind": "table.started", "table": "a", "ts": "2026-11-24T12:00:00+00:00"},
    _stage("a", "map", "waiting", "2026-11-24T12:00:10+00:00"),
    {"kind": "table.started", "table": "b", "ts": "2026-11-24T12:00:10+00:00"},
    _stage("b", "map", "waiting", "2026-11-24T12:00:30+00:00"),
    _stage("a", "fix", "active", "2026-11-24T12:00:30+00:00"),
    _stage("a", "fix", "waiting", "2026-11-24T12:00:35+00:00"),
    _stage("b", "fix", "active", "2026-11-24T12:00:35+00:00"),
]


def test_waiting_table_clock_is_paused() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    recs = {r.item: r for r in fold_events(_LEGACY)}
    a, b = recs["a"], recs["b"]
    # a: 10 s mapping + 5 s fixing, now waiting for its publish.
    assert a.status == "waiting" and a.elapsed_ms == 15000 and a.active_since is None
    # b: 20 s mapping done, fixing since 12:00:35.
    assert b.status == "running" and b.elapsed_ms == 20000
    assert b.active_since == "2026-11-24T12:00:35+00:00"
    assert [r.item for r in fold_events(_LEGACY) if r.status == "running"] == ["b"]


def test_completed_time_is_the_sum_of_active_segments() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    events = [
        *_LEGACY,
        _stage("b", "fix", "waiting", "2026-11-24T12:00:45+00:00"),
        _stage("a", "write", "active", "2026-11-24T12:00:45+00:00"),
        {"kind": "table.completed", "table": "a", "ts": "2026-11-24T12:00:47+00:00"},
        _stage("b", "write", "active", "2026-11-24T12:00:47+00:00"),
        {"kind": "table.failed", "table": "b", "ts": "2026-11-24T12:00:50+00:00", "error": "x"},
    ]
    recs = {r.item: r for r in fold_events(events)}
    assert recs["a"].status == "completed" and recs["a"].elapsed_ms == 17000
    assert recs["b"].status == "failed" and recs["b"].elapsed_ms == 33000
    assert recs["a"].active_since is None and recs["b"].active_since is None


def test_a_table_failing_while_waiting_keeps_its_active_time() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    events = [
        *_LEGACY[:2],
        {"kind": "table.failed", "table": "a", "ts": "2026-11-24T12:05:00+00:00", "error": "x"},
    ]
    (a,) = fold_events(events)
    assert a.status == "failed" and a.elapsed_ms == 10000


def test_stage_events_for_unstarted_or_finished_tables_are_ignored() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    events = [
        _stage("ghost", "map", "waiting", "2026-11-24T12:00:00+00:00"),
        *_EVENTS[1:4],
        _stage("demographic", "fix", "active", "2026-11-24T12:01:00+00:00"),
    ]
    recs = {r.item: r for r in fold_events(events)}
    assert "ghost" not in recs
    assert recs["demographic"].status == "completed"
    assert recs["demographic"].elapsed_ms == 30000


def test_saved_waiting_rows_round_trip(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    _write_log(_LEGACY)
    run_items_svc.sync_from_log(_IID)
    saved = {r.item: r for r in run_items_svc.get_items(_IID)}
    assert saved["a"].status == "waiting" and saved["a"].elapsed_ms == 15000
    assert saved["b"].active_since == "2026-11-24T12:00:35+00:00"
    rows = {r["item"]: r for r in run_items_svc.times_for_page(_IID, live=True)}
    assert rows["b"]["active_since"] == "2026-11-24T12:00:35+00:00"


def test_panel_folds_stage_pauses() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static" / "display"
    shared = (root / "run" / "panel.js").read_text(encoding="utf-8")
    assert "export function applyStageState" in shared
    assert 'kind === "stage.progress"' in shared
    assert "row.active_since" in shared and "activeSinceMs" in shared


# --- A finished run never leaves a clock ticking (2026-11-26) -------------


_WAITING_EVENTS: list[dict[str, object]] = [
    {"kind": "instance.started", "ts": "2026-11-24T12:00:00+00:00"},
    {"kind": "table.started", "table": "demographic", "ts": "2026-11-24T12:00:00+00:00"},
    {
        "kind": "stage.progress",
        "table": "demographic",
        "ts": "2026-11-24T12:00:10+00:00",
        "extra": {"state": "waiting"},
    },
    {"kind": "table.started", "table": "vital", "ts": "2026-11-24T12:00:10+00:00"},
]


def test_close_open_items_ends_running_and_waiting_items() -> None:
    from ovid2.web.services.run_items_svc import close_open_items, fold_events

    recs = {r.item: r for r in fold_events(_WAITING_EVENTS)}
    assert recs["demographic"].status == "waiting" and recs["vital"].status == "running"
    close_open_items(
        list(recs.values()), ended_at="2026-11-24T12:00:40+00:00", job_status="cancelled"
    )
    for rec in recs.values():
        assert rec.status == "cancelled" and rec.active_since is None
        assert rec.ended_at == "2026-11-24T12:00:40+00:00"
    assert recs["demographic"].elapsed_ms == 10000  # only its active time
    assert recs["vital"].elapsed_ms == 30000


def test_instance_finished_in_the_log_closes_open_items() -> None:
    from ovid2.web.services.run_items_svc import fold_events

    events = [
        *_WAITING_EVENTS,
        {
            "kind": "instance.finished",
            "ts": "2026-11-24T12:00:20+00:00",
            "extra": {"success": False},
        },
    ]
    recs = {r.item: r for r in fold_events(events)}
    assert {r.status for r in recs.values()} == {"failed"}
    assert recs["vital"].elapsed_ms == 10000


def test_finalize_run_appends_instance_finished_and_closes_rows(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    path = _write_log(_WAITING_EVENTS)
    run_items_svc.finalize_run(_IID, job_status="cancelled", ended_at="2026-11-24T12:00:40+00:00")
    tail = path.read_text(encoding="utf-8").splitlines()[-1]
    rec = json.loads(tail)
    assert rec["kind"] == "instance.finished" and rec["extra"]["job_status"] == "cancelled"
    saved = {r.item: r for r in run_items_svc.get_items(_IID)}
    assert {r.status for r in saved.values()} == {"cancelled"}
    # Idempotent: a second finalise does not append a second end event.
    run_items_svc.finalize_run(_IID, job_status="cancelled", ended_at="2026-11-24T12:00:40+00:00")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"instance.finished"' in ln for ln in lines) == 1


def test_finished_page_closes_stale_saved_rows(isolated_xdg: Path) -> None:
    from ovid2.web.services import run_items_svc

    path = _write_log(_WAITING_EVENTS)
    run_items_svc.sync_from_log(_IID)  # saved while live: rows still open
    path.unlink()
    rows = run_items_svc.times_for_page(
        _IID, live=False, job_status="failed", ended_at="2026-11-24T12:00:30+00:00"
    )
    assert rows and all(r["status"] == "failed" and r["active_since"] is None for r in rows)
    assert {r.status for r in run_items_svc.get_items(_IID)} == {"failed"}


def test_worker_finalize_closes_run_for_terminal_job(isolated_xdg: Path) -> None:
    from ovid2.web import worker
    from ovid2.web.services import run_items_svc

    _write_log(_WAITING_EVENTS)
    worker._save_run_item_times(_IID, job_status="failed", ended_at="2026-11-24T12:01:00+00:00")
    assert {r.status for r in run_items_svc.get_items(_IID)} == {"failed"}
