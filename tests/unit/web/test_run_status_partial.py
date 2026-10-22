"""Tests for the ``/partials/runs/{instance_id}/status`` HTMX poll target.

Motivated by a live incident where a browser tab kept open through a
run's ``queued -> failed`` transition still displayed ``job: queued``
even after the DB had been updated. The job-status badge on
``/runs/{id}`` is server-rendered on page load; before this poll was
added, it never refreshed. These tests lock in three things:

* the new endpoint returns the freshest job status;
* the run detail page wires it up with ``hx-get`` /
  ``hx-trigger="every …ms"`` for non-terminal jobs and omits those
  attributes for terminal jobs (so the poll doesn't spin
  indefinitely);
* the terminal-state guard script is emitted inside the partial so
  a run that transitions during the poll's lifetime halts the poll.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from ovid2.web.models import JobSubmitRequest
from ovid2.web.services import jobs_svc


@pytest.fixture()
def stub_pipeline(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Register a stub pipeline + two addresses so submit() succeeds."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_status"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]

    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/x"}}),
        encoding="utf-8",
    )
    (configs / "out_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/y"}}),
        encoding="utf-8",
    )
    yield "tp_status"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _submit(pipeline: str) -> str:
    resp = jobs_svc.submit(
        JobSubmitRequest(
            pipeline=pipeline,
            batch_id="20260101",
            input_address="in_test",
            output_address="out_test",
        )
    )
    return resp.job_id


class TestStatusPartialEndpoint:
    """The new ``GET /partials/runs/{instance_id}/status`` endpoint."""

    def test_queued_partial_reflects_queued_status(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        job_id = _submit(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        assert detail.instance_id
        response = web_client.get(  # type: ignore[attr-defined]
            f"/partials/runs/{detail.instance_id}/status"
        )
        assert response.status_code == 200
        assert "job: queued" in response.text
        # No failure banner while queued.
        assert "Failure reason" not in response.text
        # No terminal-state script while queued.
        assert "removeAttribute" not in response.text

    def test_failed_partial_reflects_failed_status_and_reason(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        instance_id = detail.instance_id or ""

        log_path = jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "# ovid2 web-worker — job=" + job_id + "\n"
            "error: Formatter not found for pipeline 'tp_status' "
            "table 'HASH_TOKEN'.\n",
            encoding="utf-8",
        )

        worker = Worker(ovid2_bin=["ovid2"])
        worker._finalize(
            _Running(
                job_id=job_id,
                instance_id=instance_id,
                process=mock.MagicMock(),
                log_handle=mock.MagicMock(),
            ),
            returncode=1,
            cancelled=False,
        )

        response = web_client.get(  # type: ignore[attr-defined]
            f"/partials/runs/{instance_id}/status"
        )
        assert response.status_code == 200
        text = response.text
        assert "job: failed" in text
        assert "Failure reason:" in text
        assert "HASH_TOKEN" in text
        # The terminal-state guard is present so the poll halts.
        assert "removeAttribute" in text
        assert 'run-job-status' in text

    def test_done_partial_emits_terminal_guard(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        """The stop-polling script is emitted for ``done`` too, not just failed."""
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        instance_id = detail.instance_id or ""

        worker = Worker(ovid2_bin=["ovid2"])
        worker._finalize(
            _Running(
                job_id=job_id,
                instance_id=instance_id,
                process=mock.MagicMock(),
                log_handle=mock.MagicMock(),
            ),
            returncode=0,
            cancelled=False,
        )

        response = web_client.get(  # type: ignore[attr-defined]
            f"/partials/runs/{instance_id}/status"
        )
        assert response.status_code == 200
        assert "job: done" in response.text
        assert "removeAttribute" in response.text
        assert "Failure reason" not in response.text

    def test_missing_instance_returns_empty_partial(
        self, isolated_xdg: Path, web_client: object,
    ) -> None:
        """Unknown instance ids return 200 with an empty body (no job)."""
        response = web_client.get(  # type: ignore[attr-defined]
            "/partials/runs/00000000-does-not-exist/status"
        )
        assert response.status_code == 200
        assert "job:" not in response.text


class TestRunDetailWiresPollForQueuedJob:
    """``run_detail.html`` wraps the status block with an HTMX poll."""

    def test_queued_run_detail_page_wires_hx_get(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        job_id = _submit(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        instance_id = detail.instance_id or ""

        response = web_client.get(f"/runs/{instance_id}")  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = response.text

        # Wrapper div is present with a stable id.
        assert 'id="run-job-status"' in body
        # And its HTMX attributes point at the new endpoint.
        assert f'hx-get="/partials/runs/{instance_id}/status"' in body
        assert 'hx-trigger="every' in body
        # Freshly-submitted job is queued.
        assert "job: queued" in body

    def test_failed_run_detail_page_omits_hx_get(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        """A page loaded for an already-failed job must not poll.

        The wrapper div still exists (so a rerun link is available)
        but the ``hx-get`` / ``hx-trigger`` attributes are omitted at
        render time, matching the terminal-state contract.
        """
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        instance_id = detail.instance_id or ""

        log_path = jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("error: something broke.\n", encoding="utf-8")

        worker = Worker(ovid2_bin=["ovid2"])
        worker._finalize(
            _Running(
                job_id=job_id,
                instance_id=instance_id,
                process=mock.MagicMock(),
                log_handle=mock.MagicMock(),
            ),
            returncode=1,
            cancelled=False,
        )

        response = web_client.get(f"/runs/{instance_id}")  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = response.text

        # Wrapper is present.
        assert 'id="run-job-status"' in body
        # But the poll attributes are absent because the job is terminal.
        assert 'hx-get="/partials/runs/' not in body.split('id="run-job-status"')[1].split(
            "</div>"
        )[0]
        assert "job: failed" in body


def _draft(pipeline: str, batch_id: str = "20260102") -> str:
    resp = jobs_svc.create_draft(
        JobSubmitRequest(
            pipeline=pipeline,
            batch_id=batch_id,
            input_address="in_test",
            output_address="out_test",
        )
    )
    return resp.job_id


class TestDraftLaunchStatusIsVisible:
    """A launched draft must not keep looking like a draft.

    Regression: after ▶ the Jobs-list row kept its ``DRAFT`` badge and the
    console label stayed draft-like for as long as the job sat queued
    behind another run (≈10 min on 2026-10-20).
    """

    def test_status_partial_emits_machine_readable_marker(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        job_id = _draft(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None and detail.status == "draft"
        url = f"/partials/runs/{detail.instance_id}/status"
        body = web_client.get(url).text  # type: ignore[attr-defined]
        assert 'data-job-status="draft"' in body

        jobs_svc.launch_draft(job_id)
        body = web_client.get(url).text  # type: ignore[attr-defined]
        assert 'data-job-status="queued"' in body
        assert 'data-queue-running="0"' in body

    def test_queue_position_counts_running_and_ahead(
        self, stub_pipeline: str,
    ) -> None:
        first = _submit(stub_pipeline)
        claimed = jobs_svc.claim_next_queued()
        assert claimed is not None and claimed.job_id == first
        ahead = _submit(stub_pipeline)
        mine = _draft(stub_pipeline)
        assert jobs_svc.queue_position(mine) is None  # draft: not in queue
        jobs_svc.launch_draft(mine)
        assert jobs_svc.queue_position(mine) == {"running": 1, "ahead": 1}
        assert jobs_svc.queue_position(first) is None  # running
        assert jobs_svc.queue_position(ahead) == {"running": 1, "ahead": 0}

    def test_run_page_shows_queue_note_after_launch(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        _submit(stub_pipeline)
        assert jobs_svc.claim_next_queued() is not None
        job_id = _draft(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        page = web_client.get(f"/runs/{detail.instance_id}").text  # type: ignore[attr-defined]
        assert "draft — ready to run" in page
        jobs_svc.launch_draft(job_id)
        page = web_client.get(f"/runs/{detail.instance_id}").text  # type: ignore[attr-defined]
        assert "draft — ready to run" not in page
        assert "queued — waiting for 1 running job" in page

    def test_list_status_endpoint_tracks_launch(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        job_id = _draft(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None and detail.instance_id
        iid = detail.instance_id
        url = f"/partials/runs/list-status?ids={iid},unknown-id"
        assert web_client.get(url).json() == {iid: "DRAFT"}  # type: ignore[attr-defined]
        jobs_svc.launch_draft(job_id)
        assert web_client.get(url).json() == {iid: "NEW"}  # type: ignore[attr-defined]
        assert jobs_svc.claim_next_queued() is not None
        assert web_client.get(url).json() == {iid: "PROCESSING"}  # type: ignore[attr-defined]

    def test_list_rows_carry_instance_id_for_badge_refresh(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        job_id = _draft(stub_pipeline)
        detail = jobs_svc.get_job(job_id)
        assert detail is not None
        page = web_client.get("/runs").text  # type: ignore[attr-defined]
        assert f'data-instance-id="{detail.instance_id}"' in page
