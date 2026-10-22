"""Unit tests for ``ovid2.web.services.jobs_svc``."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web.models import JobSubmitRequest
from ovid2.web.services import jobs_svc


@pytest.fixture()
def registered_pipeline(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a stub pipeline package with valid address configs."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_jobs"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    # Drop two address configs.
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/in"}}),
        encoding="utf-8",
    )
    (configs / "out_test.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/out"}}),
        encoding="utf-8",
    )
    yield "tp_jobs"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _make_request(pipeline: str, batch_id: str = "20260101") -> JobSubmitRequest:
    """Return a minimal valid submit request."""
    return JobSubmitRequest(
        pipeline=pipeline,
        batch_id=batch_id,
        input_address="in_test",
        output_address="out_test",
    )


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_creates_queued_row(registered_pipeline: str) -> None:
    """A successful submit inserts a queued row with a fresh instance id."""
    response = jobs_svc.submit(_make_request(registered_pipeline))
    assert response.status == "queued"
    assert response.instance_id
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.status == "queued"
    assert detail.pipeline == registered_pipeline
    assert detail.instance_id == response.instance_id
    assert detail.tables == ["all"]
    assert detail.extra_args["engine"] == "pyspark"


@pytest.mark.unit
def test_submit_writes_audit_row(registered_pipeline: str) -> None:
    """Submitting appends an audit row."""
    jobs_svc.submit(_make_request(registered_pipeline))
    with web_db.connect() as conn:
        rows = conn.execute("SELECT action, target FROM web_audit").fetchall()
    assert rows[0]["action"] == "run.submit"


@pytest.mark.unit
def test_submit_rejects_unknown_pipeline(registered_pipeline: str) -> None:
    """Unknown pipeline is rejected before touching the DB."""
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.submit(
            JobSubmitRequest(
                pipeline="not_installed",
                batch_id="X",
                input_address="in_test",
                output_address="out_test",
            )
        )


@pytest.mark.unit
def test_submit_rejects_unknown_address(registered_pipeline: str) -> None:
    """Address name that is not on the search path is rejected."""
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.submit(
            JobSubmitRequest(
                pipeline=registered_pipeline,
                batch_id="X",
                input_address="nope",
                output_address="out_test",
            )
        )


@pytest.mark.unit
def test_submit_rejects_blank_batch(registered_pipeline: str) -> None:
    """Blank batch_id is rejected."""
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.submit(
            JobSubmitRequest(
                pipeline=registered_pipeline,
                batch_id="   ",
                input_address="in_test",
                output_address="out_test",
            )
        )


@pytest.mark.unit
def test_submit_health_generic_label_with_connection_selection(
    isolated_xdg: Path,
) -> None:
    """A reusable EDC run: generic label + run-time storage selection.

    No per-partner package, no pre-saved address — the operator supplies
    a storage account (connection) + database/schema location. jobs_svc
    resolves-or-creates the addresses and enqueues the job so the worker
    can forward concrete ``--input_address`` / ``--output_address``.
    """
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "prod_disc.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": "/srv/data",
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        ),
        encoding="utf-8",
    )

    request = JobSubmitRequest(
        kind="health",
        pipeline="pcornet_edc",
        batch_id="20260101",
        input_connection="prod_disc",
        input_location="char_site/2026",
        output_connection="prod_disc",
        output_location="char_site/results",
    )
    response = jobs_svc.submit(request)
    assert response.status == "queued"

    # The request's addresses were populated by resolve-or-create.
    assert request.input_address == "prod_disc__char_site_2026"
    assert request.output_address == "prod_disc__char_site_results"

    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.pipeline == "pcornet_edc"
    assert detail.input_address == "prod_disc__char_site_2026"
    assert detail.output_address == "prod_disc__char_site_results"


@pytest.mark.unit
def test_submit_health_warehouse_empty_location_rejected(
    isolated_xdg: Path,
) -> None:
    """Reproduces the DC_CHECKS ``__root`` failure via the REST/MCP path.

    A Snowflake (warehouse) connection with no database/schema must be
    rejected at submit time — otherwise the run silently targets an empty
    database and every table read fails with "No Snowflake database
    specified", surfacing as a deceptively green health summary.
    """
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "acme-azure_eastus2.json").write_text(
        json.dumps(
            {
                "type": "snowflake",
                "args": {"account": "acme-azure_eastus2", "user": "U"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(jobs_svc.SubmitError, match="no runnable root"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="health",
                pipeline="pcornet_edc",
                batch_id="20260915-233005",
                input_connection="acme-azure_eastus2",
                input_location="",  # operator never picked a schema
                output_connection="acme-azure_eastus2",
                output_location="CHAR_ACME/RESULTS",
            )
        )
    # No __root address leaked to disk.
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    assert not list(configs.glob("acme-azure_eastus2__root.json"))


@pytest.mark.unit
def test_submit_health_generic_label_needs_no_installed_package(
    isolated_xdg: Path,
) -> None:
    """The generic ``pcornet_edc`` label is package-free for health runs."""
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "c.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": "/d", "container_path": "/data"}}),
        encoding="utf-8",
    )
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="health",
            pipeline="pcornet_edc",
            batch_id="B1",
            input_connection="c",
            input_location="db",
            output_connection="c",
            output_location="out",
        )
    )
    assert response.status == "queued"


# ---------------------------------------------------------------------------
# Copy-kind validation (ADR: copy job kind)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_copy_kind_ok(registered_pipeline: str) -> None:
    """A well-formed copy submit persists kind=copy in extra_args."""
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=registered_pipeline,
            batch_id="B_COPY",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.extra_args["kind"] == "copy"
    assert detail.tables == ["hash_token"]


@pytest.mark.unit
def test_submit_health_without_package_ok(registered_pipeline: str) -> None:
    """Health is package-free — submitting an uninstalled name must not fail.

    Regression: "Pipeline 'EDC_repot' is not installed under
    ovid2_pipelines." at run/submit time. ``EDC_repot`` is deliberately
    not the installed ``tp_jobs`` package.
    """
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="health",
            pipeline="EDC_repot",
            batch_id="B_HEALTH",
            input_address="in_test",
            output_address="out_test",
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.pipeline == "EDC_repot"
    assert detail.extra_args["kind"] == "health"


@pytest.mark.unit
def test_submit_copy_move_without_package_ok(registered_pipeline: str) -> None:
    """copy / move are package-free too — an uninstalled name is accepted."""
    for kind in ("copy", "move"):
        response = jobs_svc.submit(
            JobSubmitRequest(
                kind=kind,
                pipeline=f"nopkg_{kind}",
                batch_id="B",
                input_address="in_test",
                output_address="out_test",
                tables=["hash_token"],
                transformations=[],
            )
        )
        detail = jobs_svc.get_job(response.job_id)
        assert detail is not None
        assert detail.pipeline == f"nopkg_{kind}"


@pytest.mark.unit
def test_submit_analysis_without_package_rejected(registered_pipeline: str) -> None:
    """analysis still requires an installed package."""
    with pytest.raises(jobs_svc.SubmitError, match="is not installed under ovid2_pipelines"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="analysis",
                pipeline="not_installed",
                batch_id="X",
                input_address="in_test",
                output_address="out_test",
            )
        )


@pytest.mark.unit
def test_submit_dq_checks_package_free_forwards_cycle(registered_pipeline: str) -> None:
    """dq_checks is package-free and forwards its report cycle to the worker.

    An uninstalled pipeline label is accepted, and the chosen report
    cycle is persisted into ``extra_args['packages']`` so the worker can
    emit ``--packages`` to the (health) runner.
    """
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="dq_checks",
            pipeline="edc_unregistered",
            batch_id="B_DQ",
            input_address="in_test",
            output_address="out_test",
            packages=["20r1"],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.pipeline == "edc_unregistered"
    assert detail.extra_args["kind"] == "dq_checks"
    assert detail.extra_args["packages"] == ["20r1"]


@pytest.mark.unit
def test_submit_copy_rejects_all_tables(registered_pipeline: str) -> None:
    """`ovid2 tools copy` has no ``all`` sentinel; ``['all']`` must fail early."""
    with pytest.raises(jobs_svc.SubmitError, match="explicit `tables` list"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="copy",
                pipeline=registered_pipeline,
                batch_id="B_COPY",
                input_address="in_test",
                output_address="out_test",
                tables=["all"],
                transformations=[],
            )
        )


@pytest.mark.unit
def test_submit_copy_rejects_transformations(registered_pipeline: str) -> None:
    """Copy jobs must not carry a transformations list — they bypass the chain."""
    with pytest.raises(jobs_svc.SubmitError, match="do not accept `transformations`"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="copy",
                pipeline=registered_pipeline,
                batch_id="B_COPY",
                input_address="in_test",
                output_address="out_test",
                tables=["hash_token"],
                transformations=["format", "map"],
            )
        )


@pytest.mark.unit
def test_submit_copy_persists_extra_mounts(
    registered_pipeline: str,
    tmp_path: Path,
) -> None:
    """Well-formed extra_mounts are persisted verbatim in extra_args."""
    (tmp_path / "a").mkdir()
    mount_a = f"{tmp_path / 'a'}:/copy_out:rw"

    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=registered_pipeline,
            batch_id="B_COPY_EM",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
            extra_mounts=[mount_a],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.extra_args["extra_mounts"] == [mount_a]


@pytest.mark.unit
def test_submit_copy_rejects_bad_extra_mount(
    registered_pipeline: str,
    tmp_path: Path,
) -> None:
    """A malformed extra-mount is caught at submit time, not by the worker."""
    with pytest.raises(jobs_svc.SubmitError, match="extra_mounts"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="copy",
                pipeline=registered_pipeline,
                batch_id="B_COPY_BAD",
                input_address="in_test",
                output_address="out_test",
                tables=["hash_token"],
                transformations=[],
                extra_mounts=[f"{tmp_path}:/data"],  # reserved
            )
        )


@pytest.mark.unit
def test_submit_cdm_rejects_extra_mounts(
    registered_pipeline: str,
    tmp_path: Path,
) -> None:
    """extra_mounts is copy-kind-only; other kinds must refuse it."""
    (tmp_path / "a").mkdir()
    with pytest.raises(jobs_svc.SubmitError, match="only accepted on kind='copy'"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="cdm",
                cdm="pcornet_7_0",
                pipeline=registered_pipeline,
                batch_id="B_CDM_EM",
                input_address="in_test",
                output_address="out_test",
                extra_mounts=[f"{tmp_path / 'a'}:/copy_out"],
            )
        )


@pytest.mark.unit
def test_submit_copy_no_extra_mounts_leaves_key_absent(
    registered_pipeline: str,
) -> None:
    """When no mounts are supplied, don't sprinkle an empty list into extra_args."""
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=registered_pipeline,
            batch_id="B_COPY_NOMNT",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert "extra_mounts" not in detail.extra_args


# ---------------------------------------------------------------------------
# Claim + state transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claim_next_queued_returns_oldest(registered_pipeline: str) -> None:
    """The oldest queued row is claimed and flipped to running."""
    first = jobs_svc.submit(_make_request(registered_pipeline, batch_id="B1"))
    second = jobs_svc.submit(_make_request(registered_pipeline, batch_id="B2"))
    claimed = jobs_svc.claim_next_queued()
    assert claimed is not None
    assert claimed.job_id == first.job_id
    assert claimed.status == "running"
    # Second call claims the next.
    claimed_2 = jobs_svc.claim_next_queued()
    assert claimed_2 is not None and claimed_2.job_id == second.job_id


@pytest.mark.unit
def test_claim_returns_none_when_empty(isolated_xdg: Path) -> None:
    """Empty queue yields None."""
    web_db.ensure_schema()
    assert jobs_svc.claim_next_queued() is None


@pytest.mark.unit
def test_mark_terminal_updates_status(registered_pipeline: str) -> None:
    """Terminal transitions set ended_at and error."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(resp.job_id, status="failed", error="explode")
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    assert detail.status == "failed"
    assert detail.error == "explode"
    assert detail.ended_at is not None


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cancel_queued_transitions_to_cancelled(registered_pipeline: str) -> None:
    """A queued job cancels straight to cancelled."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    result = jobs_svc.cancel(resp.job_id)
    assert result.status == "cancelled"
    assert result.cancel_requested is True


@pytest.mark.unit
def test_cancel_running_sets_flag_only(registered_pipeline: str) -> None:
    """A running job stays running but has cancel_requested=1."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    result = jobs_svc.cancel(resp.job_id)
    assert result.status == "running"
    assert result.cancel_requested is True


@pytest.mark.unit
def test_cancel_terminal_raises(registered_pipeline: str) -> None:
    """Cancelling an already-done job raises SubmitError."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(resp.job_id, status="done")
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.cancel(resp.job_id)


# ---------------------------------------------------------------------------
# Rerun
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rerun_copies_fields_and_sets_parent(registered_pipeline: str) -> None:
    """Rerun inserts a new row with parent_job_id pointing at the original."""
    resp = jobs_svc.submit(_make_request(registered_pipeline, batch_id="B_RETRY"))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(resp.job_id, status="failed", error="x")
    new = jobs_svc.rerun(resp.job_id)
    new_detail = jobs_svc.get_job(new.job_id)
    assert new_detail is not None
    assert new_detail.parent_job_id == resp.job_id
    assert new_detail.pipeline == registered_pipeline
    assert new_detail.batch_id == "B_RETRY"
    assert new_detail.status == "queued"
    assert new_detail.instance_id != resp.instance_id


@pytest.mark.unit
def test_rerun_rejects_queued_source(registered_pipeline: str) -> None:
    """Cannot rerun a job that has not finished yet."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.rerun(resp.job_id)


@pytest.mark.unit
def test_rerun_resets_tracker_row(registered_pipeline: str) -> None:
    """When the tracker has a row for the batch, rerun flips it to NEW."""
    from ovid2.scheduler.batch_tracker import _default_sqlite_path

    tracker_path = _default_sqlite_path()
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tracker_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_batch ("
        "batch_id TEXT PRIMARY KEY, path TEXT, received_time TEXT, "
        "status TEXT, priority TEXT, processing_start TEXT, processing_end TEXT)"
    )
    conn.execute(
        "INSERT INTO data_batch VALUES ('B_RESET', '/x', '2026-01-01T00:00:00Z', "
        "'ERROR', NULL, '2026-01-01T01:00:00Z', '2026-01-01T02:00:00Z')"
    )
    conn.commit()
    conn.close()

    resp = jobs_svc.submit(_make_request(registered_pipeline, batch_id="B_RESET"))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_terminal(resp.job_id, status="failed", error="x")
    jobs_svc.rerun(resp.job_id)

    conn = sqlite3.connect(tracker_path)
    row = conn.execute(
        "SELECT status, processing_start, processing_end FROM data_batch WHERE batch_id = ?",
        ("B_RESET",),
    ).fetchone()
    conn.close()
    assert row[0] == "NEW"
    assert row[1] is None
    assert row[2] is None


# ---------------------------------------------------------------------------
# resume_in_flight
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resume_in_flight_fails_dead_pid(registered_pipeline: str) -> None:
    """Rows in running state with a dead PID transition to failed."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_running_pid(resp.job_id, 999_999_999)  # certainly dead
    reset = jobs_svc.resume_in_flight()
    assert reset == 1
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    assert detail.status == "failed"


@pytest.mark.unit
def test_resume_in_flight_cancels_when_flagged(registered_pipeline: str) -> None:
    """Running + cancel_requested rows recover as cancelled, not failed."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_running_pid(resp.job_id, 999_999_999)
    with web_db.connect() as conn:
        conn.execute("UPDATE web_jobs SET cancel_requested = 1 WHERE job_id = ?", (resp.job_id,))
    jobs_svc.resume_in_flight()
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    assert detail.status == "cancelled"


@pytest.mark.unit
def test_resume_in_flight_keeps_live_pid(registered_pipeline: str) -> None:
    """A running row whose PID is our own process stays running."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    jobs_svc.claim_next_queued()
    jobs_svc.mark_running_pid(resp.job_id, os.getpid())
    reset = jobs_svc.resume_in_flight()
    assert reset == 0
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None and detail.status == "running"


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_jobs_filters(registered_pipeline: str) -> None:
    """Filters apply to both status and pipeline."""
    a = jobs_svc.submit(_make_request(registered_pipeline, batch_id="B1"))
    jobs_svc.submit(_make_request(registered_pipeline, batch_id="B2"))
    jobs_svc.claim_next_queued()  # claim first, status=running
    jobs_svc.mark_terminal(a.job_id, status="done")

    all_jobs = jobs_svc.list_jobs()
    assert all_jobs.total == 2

    done_only = jobs_svc.list_jobs(status="done")
    assert done_only.total == 1
    assert done_only.jobs[0].status == "done"

    pipeline_filter = jobs_svc.list_jobs(pipeline=registered_pipeline)
    assert pipeline_filter.total == 2


@pytest.mark.unit
def test_get_job_by_instance(registered_pipeline: str) -> None:
    """Instance-id lookup returns the owning job."""
    resp = jobs_svc.submit(_make_request(registered_pipeline))
    detail = jobs_svc.get_job_by_instance(resp.instance_id)
    assert detail is not None
    assert detail.job_id == resp.job_id


@pytest.mark.unit
def test_worker_log_path_creates_dir(isolated_xdg: Path) -> None:
    """``worker_log_path`` creates the parent directory."""
    path = jobs_svc.worker_log_path("job-abc")
    assert path.parent.is_dir()
    assert path.name == "job-abc.log"


# ---------------------------------------------------------------------------
# Move-kind validation (M1: driver-native successor to `copy`)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_move_kind_ok(registered_pipeline: str) -> None:
    """A well-formed move submit persists kind=move in extra_args."""
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=registered_pipeline,
            batch_id="B_MOVE",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.extra_args["kind"] == "move"
    assert detail.tables == ["hash_token"]


@pytest.mark.unit
def test_submit_move_rejects_all_tables(registered_pipeline: str) -> None:
    """``ovid2 tools move`` has no ``all`` sentinel — parity with copy."""
    with pytest.raises(jobs_svc.SubmitError, match="explicit `tables` list"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="move",
                pipeline=registered_pipeline,
                batch_id="B_MOVE",
                input_address="in_test",
                output_address="out_test",
                tables=["all"],
                transformations=[],
            )
        )


@pytest.mark.unit
def test_submit_move_rejects_transformations(registered_pipeline: str) -> None:
    """Move jobs must not carry a transformations list — parity with copy."""
    with pytest.raises(jobs_svc.SubmitError, match="do not accept `transformations`"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="move",
                pipeline=registered_pipeline,
                batch_id="B_MOVE",
                input_address="in_test",
                output_address="out_test",
                tables=["hash_token"],
                transformations=["format", "map"],
            )
        )


@pytest.mark.unit
def test_submit_move_persists_strict_types(registered_pipeline: str) -> None:
    """When strict_types=True on a move job, the flag is persisted."""
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=registered_pipeline,
            batch_id="B_MOVE_STRICT",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
            strict_types=True,
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.extra_args["strict_types"] is True


@pytest.mark.unit
def test_submit_move_strict_types_default_omits_key(
    registered_pipeline: str,
) -> None:
    """Advisory-mode (default) must not persist strict_types in extra_args."""
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=registered_pipeline,
            batch_id="B_MOVE_ADV",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert "strict_types" not in detail.extra_args


@pytest.mark.unit
def test_submit_cdm_rejects_strict_types(registered_pipeline: str) -> None:
    """strict_types is move-kind-only; other kinds must refuse it."""
    with pytest.raises(jobs_svc.SubmitError, match="only accepted on kind='move'"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="cdm",
                cdm="pcornet_7_0",
                pipeline=registered_pipeline,
                batch_id="B_STRICT_ON_CDM",
                input_address="in_test",
                output_address="out_test",
                strict_types=True,
            )
        )


@pytest.mark.unit
def test_submit_copy_rejects_strict_types(registered_pipeline: str) -> None:
    """strict_types is move-only even though copy shares most other rules."""
    with pytest.raises(jobs_svc.SubmitError, match="only accepted on kind='move'"):
        jobs_svc.submit(
            JobSubmitRequest(
                kind="copy",
                pipeline=registered_pipeline,
                batch_id="B_STRICT_ON_COPY",
                input_address="in_test",
                output_address="out_test",
                tables=["hash_token"],
                transformations=[],
                strict_types=True,
            )
        )


@pytest.mark.unit
def test_submit_move_accepts_extra_mounts(
    registered_pipeline: str,
    tmp_path: Path,
) -> None:
    """extra_mounts is copy-and-move-kind; move should accept it."""
    (tmp_path / "out").mkdir()
    response = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=registered_pipeline,
            batch_id="B_MOVE_MNT",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token"],
            transformations=[],
            extra_mounts=[f"{tmp_path / 'out'}:/move_out"],
        )
    )
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None
    assert detail.extra_args["extra_mounts"] == [f"{tmp_path / 'out'}:/move_out"]


# ---------------------------------------------------------------------------
# Engine/driver family compatibility — the pre-flight check added to
# ``_validate_submit`` after the ``--engine snowpark`` incident where an
# ETL job with a disc input and Snowflake output spun up a 25-worker
# Spark cluster and then crashed at ``from snowflake.snowpark import
# Session``. See ``ovid2.drivers.engine_compat`` for background.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_rejects_incompatible_snowpark_with_postgres(
    registered_pipeline: str,
) -> None:
    """``--engine snowpark`` + postgres input rejects at submit time.

    Post-Phase-E, snowpark + disc / s3 / azure_blob / databricks are
    all compatible pairings (Phase C staging for disc via ``@~``;
    Phase D external stages for the object stores; Phase E S3-bounce
    for databricks). This test uses ``postgres`` — a driver type
    whose strategy has not yet landed on the Snowpark stager.
    """
    # Write a synthetic postgres address config for the input.
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "pg_in.json").write_text(
        json.dumps(
            {
                "src_name": "postgres",
                "src_args": {
                    "host": "db.example.com",
                    "database": "d",
                    "user": "u",
                    "schema": "public",
                },
            }
        ),
        encoding="utf-8",
    )
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806",
        input_address="pg_in",
        output_address="out_test",  # disc — compatible w/ snowpark
        engine="snowpark",
    )
    with pytest.raises(jobs_svc.SubmitError) as exc_info:
        jobs_svc.submit(request)
    message = str(exc_info.value)
    assert "snowpark" in message
    assert "pg_in" in message
    assert "--engine pyspark" in message


@pytest.mark.unit
def test_submit_accepts_snowpark_with_disc_since_phase_c(
    registered_pipeline: str,
) -> None:
    """Post-Phase-C, ``--engine snowpark`` + disc endpoints is compatible.

    :class:`SnowparkDiscStrategy` self-registers on the
    :class:`SnowparkEngineStager` at driver-package import time,
    handling the ``@~`` PUT / GET dance on the compute side. The
    compat check therefore lets the submission through — pre-Phase-C
    this same request rejected with ``SubmitError``.
    """
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806-snowpark-disc",
        input_address="in_test",  # disc
        output_address="out_test",  # disc
        engine="snowpark",
    )
    response = jobs_svc.submit(request)
    assert response.status == "queued"


@pytest.mark.unit
def test_submit_accepts_pyspark_with_disc(registered_pipeline: str) -> None:
    """``--engine pyspark`` + disc input is compatible → row inserted."""
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806-pyspark",
        input_address="in_test",
        output_address="out_test",
        engine="pyspark",
    )
    response = jobs_svc.submit(request)
    assert response.status == "queued"


@pytest.mark.unit
def test_submit_snowpark_with_snowflake_endpoints_ok(
    registered_pipeline: str,
) -> None:
    """``--engine snowpark`` + two Snowflake endpoints is compatible.

    Writes two synthetic snowflake address configs (bypassing the
    fixture's disc pair) and confirms the compat check lets the
    submission through.
    """
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "sf_in.json").write_text(
        json.dumps(
            {
                "src_name": "snowflake",
                "src_args": {"account": "acct", "user": "u"},
            }
        ),
        encoding="utf-8",
    )
    (configs / "sf_out.json").write_text(
        json.dumps(
            {
                "src_name": "snowflake",
                "src_args": {"account": "acct", "user": "u"},
            }
        ),
        encoding="utf-8",
    )
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806-sfsf",
        input_address="sf_in",
        output_address="sf_out",
        engine="snowpark",
    )
    response = jobs_svc.submit(request)
    assert response.status == "queued"


@pytest.mark.unit
def test_submit_skips_check_for_unknown_engine_name(
    registered_pipeline: str,
) -> None:
    """A custom engine name (config file, not a bare type) skips the check.

    The type-name variant only recognises the three bare type names
    (``pyspark`` / ``databricks_spark`` / ``snowpark``); anything else
    (e.g. an operator-authored engine config file) is treated as
    unknown-compatible so the pre-flight never blocks a legitimate
    named-config submission. The container will still resolve the file
    correctly via ``load_engine``.
    """
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806-custom",
        input_address="in_test",
        output_address="out_test",
        engine="my-custom-engine-config",
    )
    # Skips the check; the row is inserted. When the worker later
    # tries to ``load_engine("my-custom-engine-config")`` it will
    # fail with a concrete "engine config not found" — but that's a
    # separate concern, and correctly attributed.
    response = jobs_svc.submit(request)
    assert response.status == "queued"


@pytest.mark.unit
def test_submit_skips_check_for_unresolvable_addresses(
    registered_pipeline: str,
) -> None:
    """Address whose ``src_name`` is not in the driver registry is skipped.

    The engine-compat check specifically walks the driver registry;
    when the address exists but its ``src_name`` isn't registered
    (typically because it's a legacy shape from before the type was
    added), the check must not block the run. The outer
    ``address-on-search-path`` check still enforces existence.
    """
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "unknown_in.json").write_text(
        json.dumps({"src_name": "made_up_type", "src_args": {}}),
        encoding="utf-8",
    )
    (configs / "unknown_out.json").write_text(
        json.dumps({"src_name": "made_up_type", "src_args": {}}),
        encoding="utf-8",
    )
    request = JobSubmitRequest(
        pipeline=registered_pipeline,
        batch_id="20260806-unresolved",
        input_address="unknown_in",
        output_address="unknown_out",
        engine="snowpark",
    )
    response = jobs_svc.submit(request)
    assert response.status == "queued"


# ---------------------------------------------------------------------------
# Drafts (assistant-prepared, human-launched)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_draft_stores_draft_row_with_instance(registered_pipeline: str) -> None:
    """A draft is persisted fully specified, with an instance id, status=draft."""
    response = jobs_svc.create_draft(_make_request(registered_pipeline))
    assert response.status == "draft" and response.instance_id
    detail = jobs_svc.get_job(response.job_id)
    assert detail is not None and detail.status == "draft"
    assert detail.input_address == "in_test" and detail.output_address == "out_test"
    assert jobs_svc.get_job_by_instance(response.instance_id) is not None
    with web_db.connect() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM web_audit").fetchall()]
    assert "run.draft" in actions and "run.submit" not in actions


@pytest.mark.unit
def test_create_draft_runs_submit_validation(registered_pipeline: str) -> None:
    req = _make_request(registered_pipeline)
    req.input_address = "nope"
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.create_draft(req)


@pytest.mark.unit
def test_worker_never_claims_a_draft(registered_pipeline: str) -> None:
    """``claim_next_queued`` ignores drafts — they never execute on their own."""
    jobs_svc.create_draft(_make_request(registered_pipeline))
    assert jobs_svc.claim_next_queued() is None


@pytest.mark.unit
def test_launch_draft_queues_then_worker_claims(registered_pipeline: str) -> None:
    draft = jobs_svc.create_draft(_make_request(registered_pipeline))
    launched = jobs_svc.launch_draft(draft.job_id, actor="tester")
    assert launched.status == "queued"
    claimed = jobs_svc.claim_next_queued()
    assert claimed is not None and claimed.job_id == draft.job_id
    with web_db.connect() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM web_audit").fetchall()]
    assert "run.launch" in actions


@pytest.mark.unit
def test_launch_twice_is_refused(registered_pipeline: str) -> None:
    draft = jobs_svc.create_draft(_make_request(registered_pipeline))
    jobs_svc.launch_draft(draft.job_id)
    with pytest.raises(jobs_svc.SubmitError, match="not a draft"):
        jobs_svc.launch_draft(draft.job_id)


@pytest.mark.unit
def test_launch_non_draft_refused(registered_pipeline: str) -> None:
    queued = jobs_svc.submit(_make_request(registered_pipeline))
    with pytest.raises(jobs_svc.SubmitError, match="not a draft"):
        jobs_svc.launch_draft(queued.job_id)


@pytest.mark.unit
def test_discard_draft_deletes_and_refuses_non_draft(registered_pipeline: str) -> None:
    draft = jobs_svc.create_draft(_make_request(registered_pipeline))
    jobs_svc.discard_draft(draft.job_id)
    assert jobs_svc.get_job(draft.job_id) is None
    queued = jobs_svc.submit(_make_request(registered_pipeline))
    with pytest.raises(jobs_svc.SubmitError, match="cannot be discarded"):
        jobs_svc.discard_draft(queued.job_id)


@pytest.mark.unit
def test_draft_cannot_be_cancelled_or_rerun(registered_pipeline: str) -> None:
    draft = jobs_svc.create_draft(_make_request(registered_pipeline))
    with pytest.raises(jobs_svc.SubmitError, match="draft"):
        jobs_svc.cancel(draft.job_id)
    with pytest.raises(jobs_svc.SubmitError):
        jobs_svc.rerun(draft.job_id)


@pytest.mark.unit
def test_list_jobs_accepts_draft_filter(registered_pipeline: str) -> None:
    jobs_svc.create_draft(_make_request(registered_pipeline))
    jobs_svc.submit(_make_request(registered_pipeline))
    assert jobs_svc.list_jobs(status="draft").total == 1
