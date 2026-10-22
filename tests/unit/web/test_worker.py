"""Unit tests for ``ovid2.web.worker``."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from ovid2.web.models import JobSubmitRequest
from ovid2.web.services import jobs_svc
from ovid2.web.worker import Worker


@pytest.fixture()
def stub_pipeline(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Register a stub pipeline + two addresses so submit() succeeds."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_worker"
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
    yield "tp_worker"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _submit(pipeline: str, batch_id: str = "20260101") -> str:
    """Enqueue a job and return its id."""
    resp = jobs_svc.submit(
        JobSubmitRequest(
            pipeline=pipeline,
            batch_id=batch_id,
            input_address="in_test",
            output_address="out_test",
        )
    )
    return resp.job_id


def _wait_status(job_id: str, expected: str, timeout: float = 10.0) -> None:
    """Poll until the job reaches ``expected`` or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = jobs_svc.get_job(job_id)
        if detail is not None and detail.status == expected:
            return
        time.sleep(0.05)
    detail = jobs_svc.get_job(job_id)
    raise AssertionError(
        f"job {job_id} status={detail.status if detail else 'missing'!r} != {expected!r}"
    )


@pytest.mark.unit
def test_worker_dispatches_and_marks_done(stub_pipeline: str) -> None:
    """A queued job with a trivially-succeeding subprocess ends as ``done``."""
    job_id = _submit(stub_pipeline)
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        # ``true`` succeeds immediately; any extra args are ignored.
        ovid2_bin=["/usr/bin/true"],
    )
    worker.tick()  # spawn
    # Drain until the child exits.
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "done")


@pytest.mark.unit
def test_worker_marks_failed_on_nonzero_exit(stub_pipeline: str) -> None:
    """A subprocess exit != 0 transitions the job to ``failed``."""
    job_id = _submit(stub_pipeline, batch_id="B_FAIL")
    worker = Worker(max_parallel=1, poll_seconds=0.01, ovid2_bin=["/usr/bin/false"])
    worker.tick()
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "failed")
    detail = jobs_svc.get_job(job_id)
    assert detail is not None
    assert "exited with status" in (detail.error or "")


@pytest.mark.unit
def test_worker_respects_max_parallel(stub_pipeline: str) -> None:
    """With max_parallel=1 only one child is in flight at a time."""
    _submit(stub_pipeline, batch_id="A")
    _submit(stub_pipeline, batch_id="B")
    # ``sleep 0.2`` gives us a live child.
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        ovid2_bin=["/bin/sleep", "0.2"],
    )
    worker.tick()
    assert len(worker._running) == 1
    # A second tick before the first finishes must not spawn a second child.
    worker.tick()
    assert len(worker._running) == 1
    # Drain.
    deadline = time.time() + 3
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)


@pytest.mark.unit
def test_worker_cancels_running_child(stub_pipeline: str) -> None:
    """cancel_requested causes the worker to SIGTERM the child on next tick."""
    job_id = _submit(stub_pipeline, batch_id="B_CAN")
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        ovid2_bin=["/bin/sleep", "10"],
    )
    worker.tick()
    assert len(worker._running) == 1
    # Ask the service to cancel — sets cancel_requested=1.
    jobs_svc.cancel(job_id)
    # Next tick should SIGTERM the child.
    worker.tick()
    deadline = time.time() + 3
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "cancelled")


@pytest.mark.unit
def test_worker_cancel_stops_driver_container(
    stub_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On cancel the worker also ``docker stop``s the named driver container.

    This is the belt-and-suspenders path that guarantees the in-container
    ``ovid2 etl`` receives SIGTERM (so a Snowpark run can cancel its
    Snowflake query) even if the host-side process is wedged.
    """
    import ovid2.web.worker as worker_mod

    stops: list[list[str]] = []

    class _Completed:
        returncode = 0

    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:2] == ["docker", "stop"]:
            stops.append(list(cmd))
        return _Completed()

    monkeypatch.setattr(worker_mod.subprocess, "run", _fake_run)

    job_id = _submit(stub_pipeline, batch_id="B_CAN_DOCKER")
    worker = Worker(max_parallel=1, poll_seconds=0.01, ovid2_bin=["/bin/sleep", "10"])
    worker.tick()
    assert len(worker._running) == 1
    instance_id = worker._running[job_id].instance_id
    jobs_svc.cancel(job_id)
    # Drive the cancel handler directly (deterministic — avoids racing
    # the reaper / claim loop in a full tick).
    worker._handle_cancel_requests()
    # The docker stop runs on a daemon thread — give it a moment.
    deadline = time.time() + 3
    while not stops and time.time() < deadline:
        time.sleep(0.05)
    assert stops, "expected a docker stop for the driver container"
    argv = stops[0]
    assert f"ovid2-driver-{instance_id}" in argv
    # Clean up the still-running sleep child.
    rec = worker._running.get(job_id)
    if rec is not None:
        try:
            rec.process.terminate()
        except ProcessLookupError:
            pass
        # Close the worker's per-job log handle so the test does not leak
        # an open file descriptor (the sleep child is never reaped here).
        handle = getattr(rec, "log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


@pytest.mark.unit
def test_worker_cancel_tears_down_cluster(
    stub_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On cancel the worker also ``docker compose down``s the cluster.

    Belt-and-suspenders: the host executor's own SIGTERM handler normally
    runs ``stop_spark_cluster``, but if that process is wedged / SIGKILLed
    the master + worker containers and the project network would leak.
    The worker therefore tears the compose project down by name too.
    """
    import ovid2.web.worker as worker_mod

    downs: list[list[str]] = []

    class _Completed:
        returncode = 0

    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:3] == ["docker", "compose", "-p"]:
            downs.append(list(cmd))
        return _Completed()

    monkeypatch.setattr(worker_mod.subprocess, "run", _fake_run)

    job_id = _submit(stub_pipeline, batch_id="B_CAN_CLUSTER")
    worker = Worker(max_parallel=1, poll_seconds=0.01, ovid2_bin=["/bin/sleep", "10"])
    worker.tick()
    assert len(worker._running) == 1
    instance_id = worker._running[job_id].instance_id
    jobs_svc.cancel(job_id)
    worker._handle_cancel_requests()
    deadline = time.time() + 3
    while not downs and time.time() < deadline:
        time.sleep(0.05)
    assert downs, "expected a docker compose down for the cluster project"
    argv = downs[0]
    assert f"ovid2-{instance_id}" in argv
    assert "down" in argv
    assert "--remove-orphans" in argv
    # Clean up the still-running sleep child + its log handle.
    rec = worker._running.get(job_id)
    if rec is not None:
        try:
            rec.process.terminate()
        except ProcessLookupError:
            pass
        handle = getattr(rec, "log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


@pytest.mark.unit
def test_worker_captures_stderr_to_log(stub_pipeline: str) -> None:
    """The worker writes a per-job log file with argv header + subprocess output."""
    job_id = _submit(stub_pipeline, batch_id="B_LOG")
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        ovid2_bin=["/bin/sh", "-c", "echo hello_worker_log"],
    )
    worker.tick()
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "done")
    log = jobs_svc.worker_log_path(job_id).read_text(encoding="utf-8")
    assert "ovid2 web-worker" in log
    assert "hello_worker_log" in log


@pytest.mark.unit
def test_build_argv_includes_all_flags(stub_pipeline: str) -> None:
    """The argv builder threads every submit field through."""
    resp = jobs_svc.submit(
        JobSubmitRequest(
            pipeline=stub_pipeline,
            batch_id="B_ARGS",
            input_address="in_test",
            output_address="out_test",
            tables=["demographic", "encounter"],
            transformations=["format", "write"],
            engine="pyspark",
            workers=8,
            sf_database="DB",
            sf_account="ACCT",
            sf_schema="SCH",
            stderr_log=True,
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)
    argv_str = " ".join(argv)
    assert "--pipeline tp_worker" in argv_str
    assert "--tables demographic encounter" in argv_str
    assert "--transformations format write" in argv_str
    assert "--workers 8" in argv_str
    assert "--sf_database DB" in argv_str
    assert "--sf_account ACCT" in argv_str
    assert "--sf_schema SCH" in argv_str
    assert "--stderr-log" in argv


@pytest.mark.unit
def test_build_argv_dq_checks_uses_underscore_subcommand(stub_pipeline: str) -> None:
    """kind=dq_checks dispatches to ``ovid2 dq_checks`` (underscore) and
    carries the EDC report-cycle selection.

    Regression coverage: the worker previously emitted ``dq-checks``
    (hyphen), which the CLI's argparse rejects with
    ``invalid choice: 'dq-checks'``. Also asserts the post-refactor shape:
    ``dq_checks`` now runs the health engine, so it forwards ``--packages``
    (the report cycle) and carries no ``--transformations``.
    """
    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="dq_checks",
            pipeline=stub_pipeline,
            batch_id="B_DQ",
            input_address="in_test",
            output_address="out_test",
            packages=["20r1"],
            engine="pyspark",
            workers=25,
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None

    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)
    argv_str = " ".join(argv)

    assert argv[:2] == ["ovid2", "dq_checks"], argv
    assert "dq-checks" not in argv
    # Health-shaped: carries --packages, not --transformations.
    assert "--packages 20r1" in argv_str
    assert "--transformations" not in argv


@pytest.mark.unit
def test_build_argv_copy_kind_emits_tools_copy(stub_pipeline: str) -> None:
    """kind=copy dispatches to ``ovid2 tools copy`` with the copy-shape argv.

    Regression coverage for the ADR "Copy job kind": the worker must
    emit ``tools copy --from … --to … --tables …`` (no
    ``--transformations``, no ``--workers``, no ``--sf_*``, no
    ``--cdm``, no ``--stderr-log``) when a copy job is dispatched.
    """
    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=stub_pipeline,
            batch_id="B_COPY",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token", "external_meds"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None

    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)

    # Nested subcommand — two tokens.
    assert argv[:3] == ["ovid2", "tools", "copy"], argv
    argv_str = " ".join(argv)
    assert "--from in_test" in argv_str
    assert "--to out_test" in argv_str
    assert "--pipeline tp_worker" in argv_str
    assert "--batch_id B_COPY" in argv_str
    assert "--tables hash_token external_meds" in argv_str
    assert "--engine pyspark" in argv_str

    # None of the etl-only flags may sneak in.
    for banned in (
        "--input_address",
        "--output_address",
        "--transformations",
        "--workers",
        "--sf_database",
        "--sf_account",
        "--sf_schema",
        "--cdm",
        "--stderr-log",
    ):
        assert banned not in argv, (
            f"{banned!r} must not appear on a `tools copy` argv, got {argv!r}"
        )


@pytest.mark.unit
def test_build_argv_copy_rejects_empty_tables(stub_pipeline: str) -> None:
    """Defence-in-depth: the worker refuses copy jobs with no tables.

    Server-side validation should already have rejected these at
    submit time, but the worker also raises to prevent an argparse
    ``--tables: expected at least one argument`` from crashing the
    subprocess with a confusing error.
    """
    # We build a raw ``JobDetail`` here rather than going through
    # ``jobs_svc.submit`` because submit's own validator would reject
    # an empty tables list first — the guard we want to exercise is
    # the worker's, in case a route somehow bypassed the validator.
    from ovid2.web.models import JobDetail

    detail = JobDetail(
        job_id="j-copy-empty",
        instance_id="i-copy-empty",
        submitted_at="2026-08-05T00:00:00Z",
        submitted_by="test",
        parent_job_id=None,
        pipeline=stub_pipeline,
        batch_id="B_COPY_EMPTY",
        input_address="in_test",
        output_address="out_test",
        tables=[],
        transformations=[],
        extra_args={"kind": "copy", "engine": "pyspark"},
        status="queued",
        cancel_requested=False,
    )
    worker = Worker(ovid2_bin=["ovid2"])
    with pytest.raises(ValueError, match="require an explicit `tables` list"):
        worker._build_argv(detail)


@pytest.mark.unit
def test_build_argv_copy_forwards_extra_mounts(
    stub_pipeline: str, tmp_path: Path,
) -> None:
    """kind=copy with extra_mounts emits one ``--extra-mount HOST:CONTAINER[:MODE]`` per entry.

    The worker persists ``extra_mounts`` verbatim in ``extra_args`` at
    submit time and forwards it to ``ovid2 tools copy`` here. The
    CLI (not the worker) is the layer that validates each spec.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    mount_a = f"{tmp_path / 'a'}:/mnt_a"
    mount_b = f"{tmp_path / 'b'}:/mnt_b:ro"

    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=stub_pipeline,
            batch_id="B_COPY_EM",
            input_address="in_test",
            output_address="out_test",
            tables=["t1"],
            transformations=[],
            extra_mounts=[mount_a, mount_b],
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    assert detail.extra_args.get("extra_mounts") == [mount_a, mount_b]

    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)

    # Two --extra-mount pairs, in order, appended after the base copy argv.
    hits = [(argv[i], argv[i + 1]) for i, tok in enumerate(argv)
            if tok == "--extra-mount"]
    assert hits == [
        ("--extra-mount", mount_a),
        ("--extra-mount", mount_b),
    ], argv


@pytest.mark.unit
def test_build_argv_copy_no_extra_mounts_when_absent(
    stub_pipeline: str,
) -> None:
    """A copy job without extra_mounts must not emit stray ``--extra-mount``."""
    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="copy",
            pipeline=stub_pipeline,
            batch_id="B_COPY_NOMOUNT",
            input_address="in_test",
            output_address="out_test",
            tables=["t1"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)
    assert "--extra-mount" not in argv


@pytest.mark.unit
def test_build_env_sets_instance_id(stub_pipeline: str) -> None:
    """The worker's env carries OVID2_INSTANCE_ID for the child to pick up."""
    resp = jobs_svc.submit(
        JobSubmitRequest(
            pipeline=stub_pipeline,
            batch_id="B_ENV",
            input_address="in_test",
            output_address="out_test",
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    worker = Worker()
    env = worker._build_env(detail)
    assert env["OVID2_INSTANCE_ID"] == detail.instance_id
    # Unbuffered so host-side prints interleave with docker output in order.
    assert env["PYTHONUNBUFFERED"] == "1"


@pytest.mark.unit
def test_build_minimal_env_drops_non_allowlisted_keys() -> None:
    """Ambient credentials are stripped; only allow-listed keys survive."""
    from ovid2.web.worker import build_minimal_env

    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/ovid",
        "XDG_CONFIG_HOME": "/cfg",
        "OVID2_WEB_BIND": "x",
        "AWS_SECRET_ACCESS_KEY": "leak-me",
        "SNOWFLAKE_PASSWORD": "leak-me-too",
        "SOME_RANDOM_TOKEN": "nope",
    }
    allowlist = ("PATH", "HOME", "XDG_*", "OVID2_*")
    result = build_minimal_env(source, allowlist)

    assert result == {
        "PATH": "/usr/bin",
        "HOME": "/home/ovid",
        "XDG_CONFIG_HOME": "/cfg",
        "OVID2_WEB_BIND": "x",
    }
    assert "AWS_SECRET_ACCESS_KEY" not in result
    assert "SNOWFLAKE_PASSWORD" not in result
    assert "SOME_RANDOM_TOKEN" not in result


@pytest.mark.unit
def test_env_key_allowed_prefix_and_exact() -> None:
    """Trailing ``*`` means prefix match; otherwise exact match."""
    from ovid2.web.worker import _env_key_allowed

    allowlist = ("PATH", "OVID2_*")
    assert _env_key_allowed("PATH", allowlist) is True
    assert _env_key_allowed("PATHX", allowlist) is False  # exact only
    assert _env_key_allowed("OVID2_ANYTHING", allowlist) is True
    assert _env_key_allowed("AWS_SECRET_ACCESS_KEY", allowlist) is False


@pytest.mark.unit
def test_build_env_does_not_leak_ambient_secret(
    stub_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret exported into the worker env never reaches the child env."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    resp = jobs_svc.submit(
        JobSubmitRequest(
            pipeline=stub_pipeline,
            batch_id="B_LEAK",
            input_address="in_test",
            output_address="out_test",
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    env = Worker()._build_env(detail)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env.get("PATH") == "/usr/bin"


@pytest.mark.unit
def test_dispatch_logs_effective_mount_sources(
    stub_pipeline: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker logs effective DATA_DIR / CREDENTIALS_DIR at spawn time.

    Regression guard for the silent "FileNotFoundError" mode where the
    compose file mounted the empty XDG fallback instead of the host's
    real ``/data`` directory.
    """
    data_dir = tmp_path / "data_mount"
    data_dir.mkdir()
    creds_dir = tmp_path / "creds_mount"
    creds_dir.mkdir()
    monkeypatch.setenv("OVID2_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OVID2_CREDENTIALS_DIR", str(creds_dir))

    job_id = _submit(stub_pipeline, batch_id="B_MOUNT_LOG")
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        ovid2_bin=["/usr/bin/true"],
    )
    with caplog.at_level(logging.INFO, logger="ovid2.web.worker"):
        worker.tick()
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "done")

    dispatch_records = [
        r for r in caplog.records if "Dispatch mount sources" in r.getMessage()
    ]
    assert dispatch_records, "expected a 'Dispatch mount sources' log record"
    message = dispatch_records[0].getMessage()
    assert f"DATA_DIR={data_dir}" in message
    assert "exists=True" in message
    assert f"CREDENTIALS_DIR={creds_dir}" in message
    assert job_id in message


@pytest.mark.unit
def test_dispatch_writes_mount_sources_to_worker_log(
    stub_pipeline: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The per-job worker log header contains the effective mount sources."""
    data_dir = tmp_path / "data_mount"
    data_dir.mkdir()
    monkeypatch.setenv("OVID2_DATA_DIR", str(data_dir))
    # Intentionally do NOT create creds_dir so we can assert exists=False.
    creds_dir = tmp_path / "creds_missing"
    monkeypatch.setenv("OVID2_CREDENTIALS_DIR", str(creds_dir))

    job_id = _submit(stub_pipeline, batch_id="B_LOG_MOUNT")
    worker = Worker(
        max_parallel=1,
        poll_seconds=0.01,
        ovid2_bin=["/usr/bin/true"],
    )
    worker.tick()
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(job_id, "done")

    log_text = jobs_svc.worker_log_path(job_id).read_text(encoding="utf-8")
    assert f"DATA_DIR={data_dir} (exists=True)" in log_text
    assert f"CREDENTIALS_DIR={creds_dir} (exists=False)" in log_text


# ---------------------------------------------------------------------------
# Move-kind argv (M1: driver-native successor to `copy`)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_argv_move_kind_emits_tools_move(stub_pipeline: str) -> None:
    """kind=move dispatches to ``ovid2 tools move`` with the move-shape argv.

    Same shape as ``tools copy`` (no ``--transformations``, no
    ``--workers``, no ``--sf_*``, no ``--cdm``, no ``--stderr-log``)
    with one addition: ``--strict-types`` is emitted when the flag is
    persisted on ``extra_args``.
    """
    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=stub_pipeline,
            batch_id="B_MOVE",
            input_address="in_test",
            output_address="out_test",
            tables=["hash_token", "external_meds"],
            transformations=[],
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None

    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)

    assert argv[:3] == ["ovid2", "tools", "move"], argv
    argv_str = " ".join(argv)
    assert "--from in_test" in argv_str
    assert "--to out_test" in argv_str
    assert "--tables hash_token external_meds" in argv_str
    assert "--engine pyspark" in argv_str
    # Advisory-mode by default → no --strict-types.
    assert "--strict-types" not in argv

    # None of the etl-only flags may sneak in.
    for banned in (
        "--input_address",
        "--output_address",
        "--transformations",
        "--workers",
        "--sf_database",
        "--sf_account",
        "--sf_schema",
        "--cdm",
        "--stderr-log",
    ):
        assert banned not in argv, (
            f"{banned!r} must not appear on a `tools move` argv, got {argv!r}"
        )


@pytest.mark.unit
def test_build_argv_move_emits_strict_types(stub_pipeline: str) -> None:
    """When ``extra_args['strict_types']`` is True, the flag is emitted."""
    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=stub_pipeline,
            batch_id="B_MOVE_STRICT",
            input_address="in_test",
            output_address="out_test",
            tables=["t1"],
            transformations=[],
            strict_types=True,
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)
    assert "--strict-types" in argv


@pytest.mark.unit
def test_build_argv_move_rejects_empty_tables(stub_pipeline: str) -> None:
    """Defence-in-depth: worker refuses move jobs with no tables."""
    from ovid2.web.models import JobDetail

    detail = JobDetail(
        job_id="j-move-empty",
        instance_id="i-move-empty",
        submitted_at="2026-08-05T00:00:00Z",
        submitted_by="test",
        parent_job_id=None,
        pipeline=stub_pipeline,
        batch_id="B_MOVE_EMPTY",
        input_address="in_test",
        output_address="out_test",
        tables=[],
        transformations=[],
        extra_args={"kind": "move", "engine": "pyspark"},
        status="queued",
        cancel_requested=False,
    )
    worker = Worker(ovid2_bin=["ovid2"])
    with pytest.raises(ValueError, match="require an explicit `tables` list"):
        worker._build_argv(detail)


@pytest.mark.unit
def test_build_argv_move_forwards_extra_mounts(
    stub_pipeline: str, tmp_path: Path,
) -> None:
    """kind=move with extra_mounts emits one ``--extra-mount HOST:CONTAINER[:MODE]`` per entry."""
    (tmp_path / "a").mkdir()
    mount_a = f"{tmp_path / 'a'}:/mnt_a"

    resp = jobs_svc.submit(
        JobSubmitRequest(
            kind="move",
            pipeline=stub_pipeline,
            batch_id="B_MOVE_EM",
            input_address="in_test",
            output_address="out_test",
            tables=["t1"],
            transformations=[],
            extra_mounts=[mount_a],
        )
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None

    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)

    hits = [(argv[i], argv[i + 1]) for i, tok in enumerate(argv)
            if tok == "--extra-mount"]
    assert hits == [("--extra-mount", mount_a)], argv


# ---------------------------------------------------------------------------
# _extract_error_from_log — the worker's log-tailing helper.
#
# Motivated by the operator-visibility gap after the engine-compat fix:
# ``ovid2 tools copy --engine snowpark`` correctly aborts with rc=2 and
# a single ``error: <why>`` line on stderr, but the run detail page
# used to just show "job: failed" + a generic "exited with status 2"
# message. These tests lock in the helper that extracts the real
# reason from the tail of the merged stdout+stderr worker log.
# ---------------------------------------------------------------------------


class TestExtractErrorFromLog:
    """Tail-parsing rules for the worker log summary."""

    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "job.log"
        p.write_text(content, encoding="utf-8")
        return p

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from ovid2.web.worker import _extract_error_from_log
        assert _extract_error_from_log(tmp_path / "nope.log") is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        from ovid2.web.worker import _extract_error_from_log
        assert _extract_error_from_log(self._write(tmp_path, "")) is None

    def test_whitespace_only_returns_none(self, tmp_path: Path) -> None:
        from ovid2.web.worker import _extract_error_from_log
        assert _extract_error_from_log(self._write(tmp_path, "\n\n   \n\n")) is None

    def test_returns_last_non_empty_line(self, tmp_path: Path) -> None:
        from ovid2.web.worker import _extract_error_from_log
        content = "hello\nworld\nfinal message\n"
        assert _extract_error_from_log(self._write(tmp_path, content)) == "final message"

    def test_skips_header_comments(self, tmp_path: Path) -> None:
        """Worker header lines (``# ovid2 web-worker …``) are noise."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "the real error\n"
            "# ovid2 web-worker — job=abc\n"
            "# argv=['/usr/bin/python3', ...]\n"
            "# DATA_DIR=/data\n"
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) == "the real error"

    def test_skips_docker_compose_noise(self, tmp_path: Path) -> None:
        """Container/Network progress lines from ``docker compose up`` are skipped."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "Traceback (most recent call last):\n"
            "  File \"/app/foo.py\", line 1\n"
            "RuntimeError: real failure reason\n"
            " Network ovid2-net-20260806-000000-abcdef Removing \n"
            " Network ovid2-net-20260806-000000-abcdef Removed \n"
            " Container ovid2-master-20260806-000000-abcdef Removing \n"
            " Container ovid2-master-20260806-000000-abcdef Removed \n"
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got == "RuntimeError: real failure reason"

    def test_skips_ovid2_lifecycle_lines(self, tmp_path: Path) -> None:
        """``[ovid2] Stopping Spark cluster …`` etc. are trailing noise."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "error: some plan-time abort\n"
            "[ovid2] Starting Spark cluster (25 workers, id=...)...\n"
            "[ovid2] Spark cluster ready.\n"
            "[ovid2] Stopping Spark cluster (id=...)...\n"
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got == "error: some plan-time abort"

    def test_engine_compat_message_extracted(self, tmp_path: Path) -> None:
        """The exact CHAR_ACME → S3-Testing failure surfaces cleanly.

        This is the regression case the whole change closes: a plan-
        time abort of ``ovid2 tools copy --engine snowpark`` should
        surface its ``error: Engine 'snowpark' …`` line as the job's
        failure reason, not the generic ``exited with status 2``.
        """
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "# ovid2 web-worker — job=3a0e506f\n"
            "# argv=['/usr/bin/python3', '-m', 'ovid2.cli', 'tools', 'copy']\n"
            "# DATA_DIR=/data (exists=True)\n"
            "error: Engine 'snowpark' (family 'snowpark') is not "
            "compatible with address 'S3-Testing' (driver 's3', "
            "supports families ['spark']). Snowpark DataFrames live "
            "inside Snowflake and cannot read/write non-Snowflake "
            "peripherals; use '--engine pyspark' for cross-peripheral "
            "copies (the Bitnami base image ships the JDBC + "
            "hadoop-aws + ABFS drivers).\n"
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got is not None
        assert got.startswith("error: Engine 'snowpark'")
        assert "'--engine pyspark'" in got

    def test_returns_only_last_useful_line(self, tmp_path: Path) -> None:
        """When multiple useful lines exist, the LAST one wins."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "warning: something benign\n"
            "info: another line\n"
            "ERROR: last real complaint\n"
            " Container ovid2-master-1 Removed \n"
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got == "ERROR: last real complaint"

    def test_truncates_very_long_line(self, tmp_path: Path) -> None:
        """Lines longer than the cap are truncated with an ellipsis."""
        from ovid2.web.worker import _LOG_SUMMARY_MAX, _extract_error_from_log
        long_line = "x" * (_LOG_SUMMARY_MAX + 200)
        got = _extract_error_from_log(self._write(tmp_path, long_line + "\n"))
        assert got is not None
        assert len(got) == _LOG_SUMMARY_MAX
        assert got.endswith("\u2026")
        assert got[:10] == "x" * 10

    def test_only_reads_tail_for_huge_log(
        self, tmp_path: Path,
    ) -> None:
        """Multi-megabyte logs must not blow the memory budget."""
        from ovid2.web.worker import _LOG_TAIL_BYTES, _extract_error_from_log
        # Fabricate a 2 MB log with a useful line at the very end.
        padding = ("filler line " * 100 + "\n") * 2000  # ~2 MB
        content = padding + "the last useful line\n"
        p = self._write(tmp_path, content)
        # Sanity check: file is larger than the tail budget.
        assert p.stat().st_size > _LOG_TAIL_BYTES
        got = _extract_error_from_log(p)
        assert got == "the last useful line"

    def test_partial_first_line_of_window_discarded(
        self, tmp_path: Path,
    ) -> None:
        """The (likely partial) first line of the tail window is discarded.

        Otherwise the summary would sometimes be the middle of a
        chopped-off Spark stack frame — messier than useful.
        """
        from ovid2.web.worker import _LOG_TAIL_BYTES, _extract_error_from_log
        # Craft a log where the byte at ``size - _LOG_TAIL_BYTES``
        # falls mid-line. The first line after ``seek()`` will be a
        # partial line that must be dropped; the second line is the
        # "first real line" the helper should consider.
        prefix = "A" * (_LOG_TAIL_BYTES * 2)
        content = prefix + "\ncomplete final line\n"
        p = self._write(tmp_path, content)
        got = _extract_error_from_log(p)
        assert got == "complete final line"

    def test_invalid_utf8_is_replaced_not_fatal(self, tmp_path: Path) -> None:
        """Non-UTF-8 bytes must not crash the helper."""
        from ovid2.web.worker import _extract_error_from_log
        p = tmp_path / "job.log"
        # Valid header + invalid UTF-8 (0xff) + trailing valid text.
        p.write_bytes(b"header ok\n\xff\xfe garbage\nreal error line\n")
        got = _extract_error_from_log(p)
        assert got == "real error line"

    def test_permission_error_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unreadable log must not propagate an OSError."""
        from ovid2.web import worker as worker_module

        p = self._write(tmp_path, "some content\n")

        # Simulate ``open`` raising PermissionError.
        original_open = Path.open

        def raising_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self == p:
                raise PermissionError("simulated denial")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", raising_open)
        assert worker_module._extract_error_from_log(p) is None

    def test_only_comment_lines_returns_none(self, tmp_path: Path) -> None:
        """A log that contains only header comments has no summary."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "# ovid2 web-worker — job=abc\n"
            "# argv=[...]\n"
            "# DATA_DIR=/data\n"
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) is None

    def test_only_compose_noise_returns_none(self, tmp_path: Path) -> None:
        """A log containing only compose progress has no summary."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            " Network ovid2-net-x Creating \n"
            " Network ovid2-net-x Created \n"
            " Container ovid2-master-x Created \n"
            " Container ovid2-master-x Started \n"
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) is None

    def test_module_not_found_extracted_over_teardown_noise(
        self, tmp_path: Path,
    ) -> None:
        """The exact ETL failure from the 2026-08-06 incident.

        Actual on-disk shape: docker-compose "up" progress, then a
        Python traceback, then ``[ovid2] Stopping Spark cluster …``,
        then docker-compose "down" progress. Before the traceback
        preference pass was added, the parser would have to walk past
        ~50 compose lines and could easily land wrong. Now it just
        looks for the exception line directly.
        """
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "# ovid2 web-worker — job=647c220e\n"
            "# argv=['/usr/bin/python3', '-m', 'ovid2.cli', 'etl']\n"
            "# DATA_DIR=/data (exists=True)\n"
            ' Container ovid2-master-20260806-210407-b8f985 Creating \n'
            ' Container ovid2-master-20260806-210407-b8f985 Created \n'
            ' Container ovid2-master-20260806-210407-b8f985 Started \n'
            "Traceback (most recent call last):\n"
            '  File "/opt/bitnami/python/bin/ovid2", line 6, in <module>\n'
            "    sys.exit(main())\n"
            '  File "/app/src/ovid2/cli/__main__.py", line 137, in main\n'
            "    return int(entry(args))\n"
            '  File "/app/src/ovid2/cli/etl.py", line 124, in main\n'
            "    return _run_container_mode(args)\n"
            '  File "/app/src/ovid2/cli/etl.py", line 188, in _run_container_mode\n'
            "    spark = engine.get_session()\n"
            '  File "/app/src/ovid2/core/engines/snowpark.py", line 314, in get_session\n'
            "    from snowflake.snowpark import Session\n"
            "ModuleNotFoundError: No module named 'snowflake.snowpark'\n"
            "[ovid2] Starting Spark cluster (25 workers, id=...)...\n"
            "[ovid2] Spark cluster ready.\n"
            "[ovid2] Stopping Spark cluster (id=...)...\n"
            ' Container ovid2-master-20260806-210407-b8f985 Stopping \n'
            ' Container ovid2-master-20260806-210407-b8f985 Stopped \n'
            ' Container ovid2-master-20260806-210407-b8f985 Removing \n'
            ' Container ovid2-master-20260806-210407-b8f985 Removed \n'
            ' Network ovid2-net-20260806-210407-b8f985 Removing \n'
            ' Network ovid2-net-20260806-210407-b8f985 Removed \n'
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got is not None
        assert got.startswith("ModuleNotFoundError:")
        assert "snowflake.snowpark" in got

    def test_prefers_python_exception_over_generic_line(
        self, tmp_path: Path,
    ) -> None:
        """When both an ``error:`` line and a traceback are present, prefer the exception."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "some info line\n"
            "Traceback (most recent call last):\n"
            '  File "/tmp/x.py", line 1\n'
            "ValueError: bad thing\n"
            " Container ovid2-master-x Removed \n"
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) == "ValueError: bad thing"

    def test_recognises_java_style_exception(self, tmp_path: Path) -> None:
        """PySpark surfaces Java exceptions with fully-qualified class names."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "some info\n"
            "java.lang.IllegalArgumentException: basedir must be absolute: ?/.ivy2/local\n"
            "[ovid2] Stopping Spark cluster (id=...)...\n"
        )
        got = _extract_error_from_log(self._write(tmp_path, content))
        assert got is not None
        assert got.startswith("java.lang.IllegalArgumentException:")

    def test_takes_last_exception_when_multiple(self, tmp_path: Path) -> None:
        """A wrapped-exception chain surfaces the innermost final line."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "Traceback (most recent call last):\n"
            "KeyError: 'missing'\n"
            "\n"
            "The above exception was the direct cause of the following exception:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: wrapped\n"
        )
        # Last exception in file order — we want ``RuntimeError``.
        assert _extract_error_from_log(self._write(tmp_path, content)) == "RuntimeError: wrapped"

    def test_skips_compose_version_warning(self, tmp_path: Path) -> None:
        """The ``time="…" level=warning …`` line from docker compose is noise."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "error: some real reason\n"
            'time="2026-08-06T17:04:09-04:00" level=warning msg="version obsolete"\n'
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) == "error: some real reason"

    def test_skips_extra_lifecycle_lines(self, tmp_path: Path) -> None:
        """The ``[ovid2] Cluster not fully ready …`` and ``Shutdown reported …`` are noise."""
        from ovid2.web.worker import _extract_error_from_log
        content = (
            "error: actual reason\n"
            "[ovid2] Cluster not fully ready after timeout; proceeding anyway.\n"
            "[ovid2] Shutdown reported errors; retrying with extended timeout.\n"
        )
        assert _extract_error_from_log(self._write(tmp_path, content)) == "error: actual reason"


# ---------------------------------------------------------------------------
# _finalize integration — verify the DB row's ``error`` column gets the
# tailed message when the child exits non-zero.
# ---------------------------------------------------------------------------


class TestFinalizeSurfacesLogSummary:
    """``_finalize`` must consult the worker log for the failure reason."""

    def test_nonzero_exit_stashes_log_summary_in_job_error(
        self, stub_pipeline: str,
    ) -> None:
        """A plan-time abort message reaches ``web_jobs.error``.

        Simulates the exact operator-visible failure mode:
        ``ovid2 tools copy`` exits rc=2 after emitting a single
        ``error: <compat>`` line on stderr. The run-detail page then
        renders that message inline, no click-through required.
        """
        from unittest import mock

        from ovid2.web.services import jobs_svc as _jobs_svc
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)

        # Populate the worker log the way _finalize expects to find it.
        log_path = _jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "# ovid2 web-worker — job=" + job_id + "\n"
            "# argv=[...]\n"
            "error: Engine 'snowpark' (family 'snowpark') is not "
            "compatible with address 'S3-Testing' (driver 's3', "
            "supports families ['spark']).\n"
            " Container ovid2-master-x Removed \n",
            encoding="utf-8",
        )

        # Build a minimal _Running record and finalize with rc=2.
        worker = Worker(ovid2_bin=["ovid2"])
        fake_log_handle = mock.MagicMock()
        fake_process = mock.MagicMock()
        record = _Running(
            job_id=job_id,
            instance_id="20260806-000000-abcdef",
            process=fake_process,
            log_handle=fake_log_handle,
        )
        worker._finalize(record, returncode=2, cancelled=False)

        detail = _jobs_svc.get_job(job_id)
        assert detail is not None
        assert detail.status == _jobs_svc.STATUS_FAILED
        assert detail.error is not None
        assert detail.error.startswith("error: Engine 'snowpark'")

    def test_nonzero_exit_falls_back_when_log_has_no_summary(
        self, stub_pipeline: str,
    ) -> None:
        """When the log yields no summary, use the generic message.

        This keeps the pre-change contract for legitimate ETL crashes
        that don't emit a clean single-line ``error: …`` sentinel.
        """
        from unittest import mock

        from ovid2.web.services import jobs_svc as _jobs_svc
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)

        # An empty log yields None; _finalize must fall back.
        log_path = _jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")

        worker = Worker(ovid2_bin=["ovid2"])
        record = _Running(
            job_id=job_id,
            instance_id="20260806-000000-abcdef",
            process=mock.MagicMock(),
            log_handle=mock.MagicMock(),
        )
        worker._finalize(record, returncode=42, cancelled=False)

        detail = _jobs_svc.get_job(job_id)
        assert detail is not None
        assert detail.status == _jobs_svc.STATUS_FAILED
        assert detail.error == "ovid2 etl exited with status 42"

    def test_zero_exit_does_not_read_log(self, stub_pipeline: str) -> None:
        """Successful runs must not have a spurious error field."""
        from unittest import mock

        from ovid2.web.services import jobs_svc as _jobs_svc
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        # Even if the log contains what looks like an error, rc=0
        # should still mark the job done with error=None.
        log_path = _jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("error: ignored\n", encoding="utf-8")

        worker = Worker(ovid2_bin=["ovid2"])
        record = _Running(
            job_id=job_id,
            instance_id="20260806-000000-abcdef",
            process=mock.MagicMock(),
            log_handle=mock.MagicMock(),
        )
        worker._finalize(record, returncode=0, cancelled=False)

        detail = _jobs_svc.get_job(job_id)
        assert detail is not None
        assert detail.status == _jobs_svc.STATUS_DONE
        assert detail.error is None


# ---------------------------------------------------------------------------
# Run detail HTML — the failure-reason banner must render inline.
# ---------------------------------------------------------------------------


class TestRunDetailFailureBanner:
    """The run detail page renders ``job.error`` when the job failed."""

    def test_failure_reason_appears_on_run_detail_page(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        """The compat message reaches the page without a click-through.

        End-to-end verification of the whole change: submit a job,
        forcibly finalize it with a fabricated error line in the
        worker log, then GET ``/runs/<instance_id>`` and confirm the
        message renders inside the ``flash error`` banner.
        """
        from unittest import mock

        from ovid2.web.services import jobs_svc as _jobs_svc
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        detail = _jobs_svc.get_job(job_id)
        assert detail is not None
        assert detail.instance_id  # populated by submit()

        # Stash the compat message in the worker log.
        log_path = _jobs_svc.worker_log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "# ovid2 web-worker — job=" + job_id + "\n"
            "error: Engine 'snowpark' (family 'snowpark') is not "
            "compatible with address 'S3-Testing' (driver 's3', "
            "supports families ['spark']).\n",
            encoding="utf-8",
        )

        worker = Worker(ovid2_bin=["ovid2"])
        record = _Running(
            job_id=job_id,
            instance_id=detail.instance_id,
            process=mock.MagicMock(),
            log_handle=mock.MagicMock(),
        )
        worker._finalize(record, returncode=2, cancelled=False)

        response = web_client.get(f"/runs/{detail.instance_id}")  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = response.text
        assert "Failure reason:" in body
        # The exact message survives HTML rendering.
        assert "Engine &#39;snowpark&#39;" in body or \
               "Engine 'snowpark'" in body
        assert "S3-Testing" in body

    def test_successful_run_has_no_failure_banner(
        self, stub_pipeline: str, web_client: object,
    ) -> None:
        """The banner is gated on ``job.status == 'failed'``."""
        from unittest import mock

        from ovid2.web.services import jobs_svc as _jobs_svc
        from ovid2.web.worker import Worker, _Running

        job_id = _submit(stub_pipeline)
        detail = _jobs_svc.get_job(job_id)
        assert detail is not None

        worker = Worker(ovid2_bin=["ovid2"])
        record = _Running(
            job_id=job_id,
            instance_id=detail.instance_id or "",
            process=mock.MagicMock(),
            log_handle=mock.MagicMock(),
        )
        worker._finalize(record, returncode=0, cancelled=False)

        response = web_client.get(f"/runs/{detail.instance_id}")  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert "Failure reason:" not in response.text


@pytest.mark.unit
def test_worker_ignores_drafts_until_launched(stub_pipeline: str) -> None:
    """A draft is never dispatched; once launched it runs like any queued job."""
    from ovid2.web.models import JobSubmitRequest

    draft = jobs_svc.create_draft(JobSubmitRequest(
        pipeline=stub_pipeline, batch_id="B_DRAFT",
        input_address="in_test", output_address="out_test"))
    worker = Worker(max_parallel=1, poll_seconds=0.01, ovid2_bin=["/usr/bin/true"])
    worker.tick()
    assert not worker._running
    assert jobs_svc.get_job(draft.job_id).status == "draft"  # type: ignore[union-attr]
    jobs_svc.launch_draft(draft.job_id)
    worker.tick()
    deadline = time.time() + 5
    while worker._running and time.time() < deadline:
        worker.tick()
        time.sleep(0.05)
    _wait_status(draft.job_id, "done")
