"""HTTP tests for the W3 submit / cancel / rerun endpoints."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def pipeline_env_w3(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a stub pipeline + two address configs."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_w3"
    (pkg / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text("mappers_type='pcornet_mappers'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/i"}}), encoding="utf-8"
    )
    (configs / "out_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/o"}}), encoding="utf-8"
    )
    yield "tp_w3"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


@pytest.mark.unit
def test_submit_json_api(web_client: object, pipeline_env_w3: str) -> None:
    """POST /api/v1/runs returns 201 with job_id + instance_id."""
    payload = {
        "pipeline": pipeline_env_w3,
        "batch_id": "B1",
        "input_address": "in_test",
        "output_address": "out_test",
    }
    response = web_client.post("/api/v1/runs", json=payload)  # type: ignore[attr-defined]
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"] and body["instance_id"]


@pytest.mark.unit
def test_submit_rejects_bad_pipeline(web_client: object) -> None:
    """400 when pipeline is unknown."""
    payload = {
        "pipeline": "not_installed",
        "batch_id": "B1",
        "input_address": "in_test",
        "output_address": "out_test",
    }
    response = web_client.post("/api/v1/runs", json=payload)  # type: ignore[attr-defined]
    assert response.status_code == 400


@pytest.mark.unit
def test_list_jobs_json(web_client: object, pipeline_env_w3: str) -> None:
    """GET /api/v1/jobs surfaces the submitted job."""
    payload = {
        "pipeline": pipeline_env_w3,
        "batch_id": "B1",
        "input_address": "in_test",
        "output_address": "out_test",
    }
    web_client.post("/api/v1/runs", json=payload)  # type: ignore[attr-defined]
    response = web_client.get("/api/v1/jobs")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["jobs"][0]["pipeline"] == pipeline_env_w3


@pytest.mark.unit
def test_cancel_job_json(web_client: object, pipeline_env_w3: str) -> None:
    """POST /api/v1/jobs/{id}/cancel flips a queued job to cancelled."""
    submit = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/runs",
        json={
            "pipeline": pipeline_env_w3,
            "batch_id": "B1",
            "input_address": "in_test",
            "output_address": "out_test",
        },
    ).json()
    response = web_client.post(f"/api/v1/jobs/{submit['job_id']}/cancel")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


@pytest.mark.unit
def test_rerun_job_json(web_client: object, pipeline_env_w3: str) -> None:
    """POST /api/v1/jobs/{id}/rerun creates a new queued row."""
    from ovid2.web.services import jobs_svc

    submit = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/runs",
        json={
            "pipeline": pipeline_env_w3,
            "batch_id": "B_RR",
            "input_address": "in_test",
            "output_address": "out_test",
        },
    ).json()
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(submit["job_id"], status="failed", error="x")
    response = web_client.post(f"/api/v1/jobs/{submit['job_id']}/rerun")  # type: ignore[attr-defined]
    assert response.status_code == 201
    new = response.json()
    assert new["job_id"] != submit["job_id"]
    detail = web_client.get(f"/api/v1/jobs/{new['job_id']}").json()  # type: ignore[attr-defined]
    assert detail["parent_job_id"] == submit["job_id"]


def _rerun_setup(web_client: object, pipeline_env_w3: str) -> str:
    """Submit a job, drive it to terminal, return its job_id."""
    from ovid2.web.services import jobs_svc

    submit = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/runs",
        json={
            "pipeline": pipeline_env_w3,
            "batch_id": "B_RR_HTML",
            "input_address": "in_test",
            "output_address": "out_test",
        },
    ).json()
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(submit["job_id"], status="failed", error="x")
    return submit["job_id"]


@pytest.mark.unit
def test_rerun_html_form_redirects(web_client: object, pipeline_env_w3: str) -> None:
    """A plain form POST to /jobs/{id}/rerun 303-redirects to the new run."""
    job_id = _rerun_setup(web_client, pipeline_env_w3)
    response = web_client.post(  # type: ignore[attr-defined]
        f"/jobs/{job_id}/rerun", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs/")


@pytest.mark.unit
def test_rerun_html_fetch_returns_json(web_client: object, pipeline_env_w3: str) -> None:
    """A background fetch (X-Requested-With: fetch) gets JSON, not a 303.

    This is what the ▶ Rerun console button uses to swap the new run in
    place without a full-document reload.
    """
    job_id = _rerun_setup(web_client, pipeline_env_w3)
    response = web_client.post(  # type: ignore[attr-defined]
        f"/jobs/{job_id}/rerun",
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["instance_id"]
    assert body["job_id"] != job_id
    # The console swaps the new run's page into #main; confirm the
    # fetched run-detail page carries both the swap target and the
    # terminal pane so live.js can re-boot after the in-place swap.
    page = web_client.get(f"/runs/{body['instance_id']}")  # type: ignore[attr-defined]
    assert page.status_code == 200
    assert 'id="main"' in page.text
    assert "terminal-pane" in page.text


@pytest.mark.unit
def test_submit_html_redirects(web_client: object, pipeline_env_w3: str) -> None:
    """POST /submit form redirects to the run detail page."""
    response = web_client.post(  # type: ignore[attr-defined]
        "/submit",
        data={
            "pipeline": pipeline_env_w3,
            "batch_id": "B_HTML",
            "input_address": "in_test",
            "output_address": "out_test",
            "tables": "all",
            "transformations": "all",
            "engine": "pyspark",
            "workers": "25",
            "sf_database": "",
            "sf_account": "",
            "sf_schema": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/runs/" in response.headers["location"]


@pytest.mark.unit
def test_submit_html_error_redirect(web_client: object) -> None:
    """Invalid submit form redirects back to /submit with ?error=."""
    response = web_client.post(  # type: ignore[attr-defined]
        "/submit",
        data={
            "pipeline": "no_such",
            "batch_id": "B",
            "input_address": "nope",
            "output_address": "nope",
            "tables": "",
            "transformations": "",
            "engine": "pyspark",
            "workers": "25",
            "sf_database": "",
            "sf_account": "",
            "sf_schema": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/submit?error=" in response.headers["location"]


@pytest.mark.unit
def test_jobs_redirects_to_runs(web_client: object, pipeline_env_w3: str) -> None:
    """Legacy /jobs 302-redirects into the merged runs workspace."""
    resp = web_client.get(  # type: ignore[attr-defined]
        "/jobs", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/runs"


@pytest.mark.unit
def test_runs_workspace_lists_batch(web_client: object, pipeline_env_w3: str) -> None:
    """The merged /runs workspace renders and includes the batch id."""
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/runs",
        json={
            "pipeline": pipeline_env_w3,
            "batch_id": "B_JOBS",
            "input_address": "in_test",
            "output_address": "out_test",
        },
    )
    response = web_client.get("/runs")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "B_JOBS" in response.text
    # Master-detail workspace markers.
    assert 'class="run-list"' in response.text
    assert 'id="run-detail-region"' in response.text


@pytest.mark.unit
def test_submit_page_renders(web_client: object, pipeline_env_w3: str) -> None:
    """The submit form lists pipelines + addresses."""
    response = web_client.get("/submit")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert pipeline_env_w3 in response.text
    assert "in_test" in response.text
