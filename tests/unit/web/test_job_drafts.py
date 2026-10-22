"""HTTP tests for draft jobs (assistant-prepared, human-launched).

A draft is created by ``POST /api/v1/job-drafts`` (the ``create_job_draft``
agent tool, executed by the user's browser without a click), rendered on
``/runs/<instance_id>`` with a breathing ▶ (``.run-ready``) and only
executed once a human launches it (``POST /jobs/{id}/launch``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def draft_env(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a stub pipeline, two addresses and an open dq_checks config."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_draft"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text("mappers_type='pcornet_mappers'\n",
                                              encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    for name in ("in_test", "out_test"):
        (configs / f"{name}.json").write_text(
            json.dumps({"src_name": "disc", "src_args": {"path": f"/tmp/{name}"}}),
            encoding="utf-8")
    yield "tp_draft"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _draft_payload(pipeline: str) -> dict[str, object]:
    return {"kind": "dq_checks", "pipeline": pipeline, "batch_id": "20261020",
            "input_address": "in_test", "output_address": "out_test",
            "packages": ["dcq_v7_03"]}


@pytest.mark.unit
def test_create_draft_api_and_run_page(web_client: object, draft_env: str) -> None:
    resp = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env))  # type: ignore[attr-defined]
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft" and body["instance_id"]

    page = web_client.get(f"/runs/{body['instance_id']}")  # type: ignore[attr-defined]
    assert page.status_code == 200
    text = page.text
    # Breathing ▶ launch control + discard + populated run details.
    assert 'data-action="launch"' in text
    assert "run-ready" in text
    assert f'/jobs/{body["job_id"]}/launch' in text
    assert f'/jobs/{body["job_id"]}/discard' in text
    assert 'data-status="draft"' in text
    assert "dq_checks" in text and "dcq_v7_03" in text
    # No cancel/stop control for a draft.
    assert 'data-action="cancel"' not in text
    # Listed as DRAFT in the Jobs list.
    assert "status-draft" in text


@pytest.mark.unit
def test_launch_api_queues_draft(web_client: object, draft_env: str) -> None:
    body = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env)).json()  # type: ignore[attr-defined]
    resp = web_client.post(f"/api/v1/jobs/{body['job_id']}/launch")  # type: ignore[attr-defined]
    assert resp.status_code == 200 and resp.json()["status"] == "queued"
    again = web_client.post(f"/api/v1/jobs/{body['job_id']}/launch")  # type: ignore[attr-defined]
    assert again.status_code == 400


@pytest.mark.unit
def test_launch_html_fetch_returns_json_and_form_redirects(
    web_client: object, draft_env: str
) -> None:
    a = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env)).json()  # type: ignore[attr-defined]
    r = web_client.post(f"/jobs/{a['job_id']}/launch",  # type: ignore[attr-defined]
                        headers={"X-Requested-With": "fetch"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["instance_id"] == a["instance_id"]
    page = web_client.get(f"/runs/{a['instance_id']}")  # type: ignore[attr-defined]
    assert 'data-action="cancel"' in page.text and "run-ready" not in page.text.split(
        'class="terminal-pane')[1]

    b = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env)).json()  # type: ignore[attr-defined]
    r2 = web_client.post(f"/jobs/{b['job_id']}/launch", follow_redirects=False)  # type: ignore[attr-defined]
    assert r2.status_code == 303 and r2.headers["location"] == f"/runs/{b['instance_id']}"


@pytest.mark.unit
def test_discard_api_and_html(web_client: object, draft_env: str) -> None:
    a = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env)).json()  # type: ignore[attr-defined]
    assert web_client.post(f"/api/v1/jobs/{a['job_id']}/discard").status_code == 204  # type: ignore[attr-defined]
    assert web_client.get(f"/api/v1/jobs/{a['job_id']}").status_code == 404  # type: ignore[attr-defined]
    b = web_client.post("/api/v1/job-drafts", json=_draft_payload(draft_env)).json()  # type: ignore[attr-defined]
    r = web_client.post(f"/jobs/{b['job_id']}/discard", follow_redirects=False)  # type: ignore[attr-defined]
    assert r.status_code == 303 and r.headers["location"] == "/runs"


@pytest.mark.unit
def test_draft_api_validates_like_submit(web_client: object, draft_env: str) -> None:
    bad = dict(_draft_payload(draft_env), input_address="nope")
    resp = web_client.post("/api/v1/job-drafts", json=bad)  # type: ignore[attr-defined]
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_pipeline_config_binds_open_sides_and_drafts(
    web_client: object, draft_env: str
) -> None:
    """An open-input dq_checks config runs with address overrides (A) or as a draft."""
    pipelines = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "pipelines"
    pipelines.mkdir(parents=True, exist_ok=True)
    (pipelines / "DCX.json").write_text(json.dumps({
        "kind": "dq_checks", "cdm": None, "pipeline": "DCX", "input_address": "",
        "output_address": "", "tables": ["all"], "transformations": [],
        "packages": ["dcq_v7_03"], "engine": "pyspark", "workers": 25,
        "enabled": True, "trigger": {"type": "manual"}}), encoding="utf-8")

    open_run = web_client.post("/api/v1/pipelines/DCX/run", json={})  # type: ignore[attr-defined]
    assert open_run.status_code == 400 and "open" in open_run.json()["detail"]

    resp = web_client.post("/api/v1/pipelines/DCX/run", json={  # type: ignore[attr-defined]
        "input_address": "in_test", "output_address": "out_test", "draft": True})
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft"
    job = web_client.get(f"/api/v1/jobs/{resp.json()['job_id']}").json()  # type: ignore[attr-defined]
    assert job["input_address"] == "in_test" and job["output_address"] == "out_test"
    assert job["extra_args"]["packages"] == ["dcq_v7_03"]

    queued = web_client.post("/api/v1/pipelines/DCX/run", json={  # type: ignore[attr-defined]
        "input_address": "in_test", "output_address": "out_test"})
    assert queued.status_code == 201 and queued.json()["status"] == "queued"


@pytest.mark.unit
def test_draft_rejects_json_location(web_client: object, draft_env: str) -> None:
    """A model passing a JSON blob as *_location gets a clean 400, no junk address."""
    payload = {"kind": "dq_checks", "pipeline": draft_env, "batch_id": "1",
               "input_connection": "anything",
               "input_location": '{"database": "CHAR_ACME", "schema": "PUBLIC"}',
               "output_address": "out_test"}
    resp = web_client.post("/api/v1/job-drafts", json=payload)  # type: ignore[attr-defined]
    assert resp.status_code == 400 and "plain path" in resp.json()["detail"]


@pytest.mark.unit
def test_draft_pipeline_config_route(web_client: object, draft_env: str) -> None:
    """``POST /pipelines/{name}/draft`` fills open sides and always drafts."""
    pipelines = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "pipelines"
    pipelines.mkdir(parents=True, exist_ok=True)
    (pipelines / "DCY.json").write_text(json.dumps({
        "kind": "dq_checks", "cdm": None, "pipeline": "DCY", "input_address": "",
        "output_address": "out_test", "tables": ["all"], "transformations": [],
        "packages": ["dcq_v7_03"], "engine": "pyspark", "workers": 25,
        "enabled": True, "trigger": {"type": "manual"}}), encoding="utf-8")
    assert web_client.post("/api/v1/pipelines/DCY/draft", json={}).status_code == 400  # type: ignore[attr-defined]
    resp = web_client.post("/api/v1/pipelines/DCY/draft", json={  # type: ignore[attr-defined]
        "input_address": "in_test"})
    assert resp.status_code == 201 and resp.json()["status"] == "draft"
    from ovid2.web.services import jobs_svc

    assert jobs_svc.claim_next_queued() is None
