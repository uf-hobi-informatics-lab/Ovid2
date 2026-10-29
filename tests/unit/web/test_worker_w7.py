"""Tests for the W7 worker.dispatch_due_pipelines hook."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ovid2.web import worker as worker_module
from ovid2.web.models import PipelineTrigger, PipelineWrite
from ovid2.web.services import pipelines_svc


def _prime(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ovid2.web.services.pipelines_svc.pipelines_meta_svc.discover_pipelines",
        lambda: ["ptr"],
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "in.json").write_text('{"src_name": "disc", "src_args": {"path": "/tmp/in"}}')
    (configs / "out.json").write_text('{"src_name": "disc", "src_args": {"path": "/tmp/out"}}')


@pytest.mark.unit
def test_dispatch_fires_due_interval_route(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(isolated_xdg, monkeypatch)
    payload = PipelineWrite(
        pipeline="ptr",
        input_address="in",
        output_address="out",
        trigger=PipelineTrigger(type="interval", interval_minutes=1),
    )
    pipelines_svc.write_pipeline("r_int", payload, actor="test", create_only=True)
    # Force next_fire_at into the past so the tick fires immediately.
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    from ovid2.web import db as web_db

    pipelines_svc._ensure_state_schema()
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_pipelines_state SET next_fire_at = ? WHERE name = ?",
            (past, "r_int"),
        )
    submitted: list[Any] = []
    monkeypatch.setattr(
        pipelines_svc,
        "run_now",
        lambda name, actor="worker": submitted.append((name, actor)) or _fake_response(),
    )
    w = worker_module.Worker(max_parallel=0, poll_seconds=0.1)
    w._dispatch_due_pipelines()
    assert submitted == [("r_int", "worker")]


@pytest.mark.unit
def test_dispatch_respects_disabled_flag(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(isolated_xdg, monkeypatch)
    from ovid2.web.models import GlobalSettings, SettingsWorker
    from ovid2.web.services import settings_svc

    settings_svc.invalidate_cache()
    settings_svc.save_settings(
        GlobalSettings(worker=SettingsWorker(route_dispatch_enabled=False)),
        actor="test",
    )
    payload = PipelineWrite(
        pipeline="ptr",
        input_address="in",
        output_address="out",
        trigger=PipelineTrigger(type="interval", interval_minutes=1),
    )
    pipelines_svc.write_pipeline("r_off", payload, actor="test", create_only=True)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    from ovid2.web import db as web_db

    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_pipelines_state SET next_fire_at = ? WHERE name = ?",
            (past, "r_off"),
        )
    submitted: list[Any] = []
    monkeypatch.setattr(
        pipelines_svc,
        "run_now",
        lambda name, actor="worker": submitted.append((name, actor)) or _fake_response(),
    )
    w = worker_module.Worker(max_parallel=0, poll_seconds=0.1)
    w._dispatch_due_pipelines()
    assert submitted == []


def _fake_response() -> Any:
    """Return a minimal object with the attrs run_now callers read."""
    from ovid2.web.models import JobSubmitResponse

    return JobSubmitResponse(job_id="fake", instance_id="fake", status="queued")
