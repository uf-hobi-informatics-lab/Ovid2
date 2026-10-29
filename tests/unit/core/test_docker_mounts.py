"""Unit tests for the ``docker run`` command builder.

Regression guards for the conditional ``LEGACY_CREDENTIALS_DIR`` mount in
``build_docker_command``. Docker rejects the ``docker run`` invocation
when a bind-mount source does not exist, so hosts that don't have a
``/ovid_credentials`` directory (dev laptops, hosts using
``/usr/include/rsa`` instead) must not have that ``-v`` flag emitted.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

import ovid2.core.docker as docker_module
from ovid2.core.isolation import OvidInstance


@pytest.fixture(autouse=True)
def _stub_nss_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return static paths from ``ensure_nss_files`` so tests stay hermetic.

    ``build_docker_command`` now calls
    :meth:`OvidInstance.ensure_nss_files` to materialise the per-
    invocation ``passwd`` / ``group`` overlay that lets the container
    run as the host uid. Stub it out — this test file only cares
    about the argv shape.
    """
    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/tmp/passwd"), Path("/tmp/group")),
    )
    # ``build_docker_command`` also calls ``ensure_directories`` now
    # (to materialise the per-invocation Spark work / logs bind sources
    # so docker doesn't reject the ``docker run`` invocation). Stub it
    # so the tests don't touch the host filesystem.
    monkeypatch.setattr(
        OvidInstance,
        "ensure_directories",
        lambda self: None,
    )


def _make_args() -> Namespace:
    """Minimal argparse-shaped args accepted by ``build_docker_command``."""
    return Namespace(
        command="etl",
        input_address="in",
        output_address="out",
        pipeline="test",
        batch_id="20260101",
        tables=["demographic"],
        transformations=["format"],
        engine="pyspark",
    )


@pytest.mark.unit
def test_build_docker_command_mounts_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DATA_DIR is always mounted at /data:ro."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    assert f"{data_dir}:/data:ro" in cmd


@pytest.mark.unit
def test_build_docker_command_mounts_credentials_when_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LEGACY_CREDENTIALS_DIR is mounted when the source directory exists."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", creds_dir)

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    assert f"{creds_dir}:/ovid_credentials:ro" in cmd


@pytest.mark.unit
def test_build_docker_command_skips_credentials_when_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing LEGACY_CREDENTIALS_DIR must NOT emit a mount entry.

    Otherwise docker would refuse to launch with
    ``invalid mount config for type "bind": bind source path does not exist``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    absent_creds = tmp_path / "absent"
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", absent_creds)

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    assert "/ovid_credentials" not in " ".join(cmd)
    assert str(absent_creds) not in " ".join(cmd)


@pytest.mark.unit
def test_build_docker_command_mounts_spark_work_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-invocation Spark work + logs dirs are bound onto the driver.

    Root cause guarded here: the image's ``$SPARK_HOME/{work,logs}``
    (``/opt/spark/…``) may not be writable for the host uid. Any
    plain-Python ``SparkSession.builder.getOrCreate()`` inside the
    driver container that then forks executors (local mode) or that
    tries to open a Spark log file would fail with
    ``IOException: Failed to create directory
    /opt/spark/work/…`` when running as ``--user $UID:$GID``.
    Fix: bind-mount the per-invocation
    :attr:`OvidInstance.spark_work_dir` and
    :attr:`OvidInstance.spark_logs_dir` at both container paths (rw).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    joined = " ".join(cmd)
    assert f"{instance.spark_work_dir}:/opt/spark/work:rw" in joined, (
        f"missing spark_work_dir bind in argv: {joined!r}"
    )
    assert f"{instance.spark_logs_dir}:/opt/spark/logs:rw" in joined, (
        f"missing spark_logs_dir bind in argv: {joined!r}"
    )


@pytest.mark.unit
def test_build_docker_command_mounts_work_dir_at_same_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Host ``WORK_DIR`` is bind-mounted into the container at the same path.

    Root cause guarded here: the driver runs inside the container and
    writes the JSONL event log (including every ``engine.metrics``
    sample from the live sampler) to
    ``ovid2.config.settings.WORK_DIR / f'events-{id}.jsonl'``. Inside
    the container WORK_DIR falls back to ``/tmp/ovid2-<uid>/ovid2/``
    (no ``$XDG_RUNTIME_DIR`` in the image) so the write vanishes with
    the container. Meanwhile the host's web tier tails
    ``$XDG_RUNTIME_DIR/ovid2/events-<id>.jsonl`` and sees nothing —
    the run-detail page's live tiles stay dark for the entire job.
    Fix: bind-mount the host WORK_DIR at the same absolute path *and*
    set ``OVID2_WORK_DIR`` so the container-side computation resolves
    identically. See docker.py:build_docker_command for the block.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fake_work = tmp_path / "runtime" / "ovid2"
    fake_work.mkdir(parents=True)
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")
    monkeypatch.setattr(docker_module, "WORK_DIR", fake_work)

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    joined = " ".join(cmd)

    # Same-path bind mount so instance.event_log resolves identically
    # on host and inside container. Must be rw — the sampler appends
    # every 500ms on the driver side.
    assert f"{fake_work}:{fake_work}:rw" in joined, f"missing WORK_DIR bind in argv: {joined!r}"

    # Env var propagation so config/settings.py:WORK_DIR resolves to
    # the mounted path inside the container (rather than falling back
    # to /tmp/ovid2-<uid>/ovid2/ via _xdg_runtime_dir()).
    assert f"OVID2_WORK_DIR={fake_work}" in cmd, f"missing OVID2_WORK_DIR env in argv: {cmd!r}"


# ---------------------------------------------------------------------------
# _build_ovid2_argv dispatch (ADR: copy job kind)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_ovid2_argv_etl_shape() -> None:
    """The etl branch preserves the historical single-token subcommand + flags."""
    args = Namespace(
        command="etl",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="B",
        tables=["t1", "t2"],
        transformations=["format", "map"],
        engine="pyspark",
        sf_database="DB",
    )
    argv = docker_module._build_ovid2_argv(args)
    assert argv[0] == "etl"
    assert "--input_address" in argv and "in" in argv
    assert "--tables" in argv and "t1" in argv and "t2" in argv
    assert "--transformations" in argv
    assert "--sf_database" in argv and "DB" in argv


@pytest.mark.unit
def test_build_ovid2_argv_dq_checks_health_shape() -> None:
    """dq_checks is health-shaped: keeps its own token, emits --packages.

    It reuses the health argv builder (no --transformations) but must lead
    with the ``dq_checks`` token so the CLI parser accepts it and the
    ``/data`` mount stays read-only.
    """
    args = Namespace(
        command="dq_checks",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="B",
        engine="pyspark",
        packages=["20r1"],
    )
    argv = docker_module._build_ovid2_argv(args)
    assert argv[0] == "dq_checks"
    assert "--packages" in argv and "20r1" in argv
    assert "--transformations" not in argv


@pytest.mark.unit
def test_build_ovid2_argv_health_emits_packages() -> None:
    """The health builder forwards --packages (regression: it was dropped)."""
    args = Namespace(
        command="health",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="B",
        engine="pyspark",
        packages=["20r1", "20r2"],
    )
    argv = docker_module._build_ovid2_argv(args)
    assert argv[0] == "health"
    assert "--packages" in argv and "20r1" in argv and "20r2" in argv


@pytest.mark.unit
def test_build_ovid2_argv_tools_copy_shape() -> None:
    """The copy branch emits ``tools copy --from --to --tables --engine`` only."""
    args = Namespace(
        command="tools copy",
        from_address="in",
        to_address="out",
        pipeline="p",
        batch_id="B",
        tables=["hash_token", "external_meds"],
        engine="pyspark",
    )
    argv = docker_module._build_ovid2_argv(args)
    assert argv[:2] == ["tools", "copy"]
    argv_str = " ".join(argv)
    assert "--from in" in argv_str
    assert "--to out" in argv_str
    assert "--tables hash_token external_meds" in argv_str
    assert "--engine pyspark" in argv_str
    # None of the etl-only flags leak in.
    for banned in (
        "--input_address",
        "--output_address",
        "--transformations",
        "--sf_database",
        "--sf_account",
        "--sf_schema",
    ):
        assert banned not in argv, f"{banned!r} must not appear on the tools-copy argv"


@pytest.mark.unit
def test_build_ovid2_argv_tools_copy_accepts_input_address_alias() -> None:
    """When called through ``ovid2 etl``-shaped code that only knows about
    ``input_address`` / ``output_address``, ``_build_tools_copy_argv`` still
    finds the address names via the alias fall-back."""
    args = Namespace(
        command="tools copy",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="B",
        tables=["T"],
        engine="pyspark",
    )
    argv = docker_module._build_ovid2_argv(args)
    argv_str = " ".join(argv)
    assert "--from in" in argv_str
    assert "--to out" in argv_str


@pytest.mark.unit
def test_build_docker_command_emits_two_tokens_for_tools_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: build_docker_command splats the two-token ``tools copy``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")

    args = Namespace(
        command="tools copy",
        from_address="in",
        to_address="out",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="B",
        tables=["hash_token"],
        transformations=[],  # ignored
        engine="pyspark",
        sf_database=None,
        sf_account=None,
        sf_schema=None,
    )
    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(args, instance)
    joined = " ".join(cmd)
    # The two tokens appear in the right order after the image name.
    assert "tools copy --from in --to out" in joined


@pytest.mark.unit
def test_build_ovid2_argv_tools_copy_roundtrips_split_form() -> None:
    """The ``LOGICAL=SOURCE`` form must round-trip host→container verbatim.

    The container-side argparse re-parses ``args.tables``, so the
    host side must not mangle the ``=`` or the glob metacharacters
    when splatting through ``*args.tables``.
    """
    args = Namespace(
        command="tools copy",
        from_address="in",
        to_address="out",
        pipeline="p",
        batch_id="B",
        tables=[
            "DEMOGRAPHIC",
            "HASH_TOKEN=*datavant_person_tokens*",
            "ENCOUNTER",
        ],
        engine="pyspark",
    )
    argv = docker_module._build_ovid2_argv(args)
    # ``--tables`` followed by the entries, in order, byte-preserved.
    tables_idx = argv.index("--tables")
    assert argv[tables_idx + 1] == "DEMOGRAPHIC"
    assert argv[tables_idx + 2] == "HASH_TOKEN=*datavant_person_tokens*"
    assert argv[tables_idx + 3] == "ENCOUNTER"


@pytest.mark.unit
def test_build_ovid2_argv_tools_move_roundtrips_split_form() -> None:
    """Move argv must round-trip ``LOGICAL=SOURCE`` verbatim."""
    args = Namespace(
        command="tools move",
        from_address="in",
        to_address="out",
        pipeline="p",
        batch_id="B",
        tables=["HASH_TOKEN=*datavant_person_tokens*"],
        engine="pyspark",
        strict_types=False,
    )
    argv = docker_module._build_ovid2_argv(args)
    tables_idx = argv.index("--tables")
    assert argv[tables_idx + 1] == "HASH_TOKEN=*datavant_person_tokens*"


# ---------------------------------------------------------------------------
# Driver container naming + cancel-time ``docker stop``
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_driver_container_name_is_deterministic() -> None:
    """The driver name is ``ovid2-driver-<instance_id>``."""
    name = docker_module.driver_container_name("20260101-000000-abcdef")
    assert name == "ovid2-driver-20260101-000000-abcdef"


@pytest.mark.unit
def test_driver_container_name_sanitises_bad_chars() -> None:
    """Non-name-safe characters collapse to ``-``."""
    name = docker_module.driver_container_name("weird id/with*chars")
    assert name == "ovid2-driver-weird-id-with-chars"
    # Result is a valid docker --name (alnum / _ / . / -).
    assert all(c.isalnum() or c in "._-" for c in name)


@pytest.mark.unit
def test_build_docker_command_names_driver_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``docker run`` carries ``--name ovid2-driver-<instance_id>`` so a
    cancel path can address the container by name."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    assert "--name" in cmd
    name_idx = cmd.index("--name")
    assert cmd[name_idx + 1] == "ovid2-driver-20260101-000000-abcdef"


@pytest.mark.unit
def test_stop_driver_container_issues_docker_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop_driver_container`` shells out ``docker stop <name>``."""
    calls: list[list[str]] = []

    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _Completed()

    class _Completed:
        returncode = 0

    monkeypatch.setattr(docker_module.subprocess, "run", _fake_run)
    docker_module.stop_driver_container("20260101-000000-abcdef")
    assert calls, "expected a docker stop call"
    argv = calls[0]
    assert argv[0] == "docker" and argv[1] == "stop"
    assert "ovid2-driver-20260101-000000-abcdef" in argv


@pytest.mark.unit
def test_stop_driver_container_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A docker failure during stop must not raise (cancel is best-effort)."""

    def _boom(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        raise OSError("docker not found")

    monkeypatch.setattr(docker_module.subprocess, "run", _boom)
    # Must not raise.
    docker_module.stop_driver_container("20260101-000000-abcdef")


@pytest.mark.unit
def test_submit_to_docker_stops_driver_on_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM to the host process stops the named driver container.

    Reproduces the cancel path: the worker SIGTERMs the host ``ovid2``
    process; its handler must ``docker stop`` the driver container so the
    in-container ``ovid2 etl`` receives SIGTERM and can cancel its
    Snowflake query.
    """
    import signal as _signal

    stopped: list[str] = []

    monkeypatch.setattr(
        docker_module,
        "build_docker_command",
        lambda args, instance: ["docker", "run", "--rm", "sleep"],
    )
    monkeypatch.setattr(
        docker_module,
        "stop_driver_container",
        lambda instance_id, **kw: stopped.append(instance_id),
    )

    # Fire SIGTERM while ``subprocess.run`` is "executing" the container.
    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        handler = _signal.getsignal(_signal.SIGTERM)
        # Simulate the worker delivering SIGTERM to this process.
        try:
            handler(_signal.SIGTERM, None)  # type: ignore[misc]
        except SystemExit:
            raise
        return _Completed()

    class _Completed:
        returncode = 0

    monkeypatch.setattr(docker_module.subprocess, "run", _fake_run)

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    with pytest.raises(SystemExit):
        docker_module.submit_to_docker(_make_args(), instance)
    assert stopped == ["20260101-000000-abcdef"]


# ---------------------------------------------------------------------------
# Executor least-privilege: scoped secret mounts
# (harness / API / executor trust model)
# ---------------------------------------------------------------------------


def _prepare_xdg_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refs: list[str]) -> Path:
    """Create an XDG config tree with a secrets.d/ holding ``refs``.json."""
    xdg = tmp_path / "xdg"
    (xdg / "secrets.d").mkdir(parents=True)
    for ref in refs:
        (xdg / "secrets.d" / f"{ref}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


@pytest.mark.unit
def test_secrets_dir_is_shadowed_by_tmpfs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The whole secrets.d/ is never left readable — it is tmpfs-shadowed."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")
    _prepare_xdg_secrets(monkeypatch, tmp_path, ["alpha", "beta"])
    monkeypatch.setattr(
        docker_module, "secret_refs_for_addresses", lambda names: set(), raising=False
    )

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    # tmpfs shadow present.
    assert "--tmpfs" in cmd
    assert "/ovid2-user-config/secrets.d" in cmd


@pytest.mark.unit
def test_only_referenced_secret_is_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only the run's referenced secret file is bound back over the tmpfs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")
    xdg = _prepare_xdg_secrets(monkeypatch, tmp_path, ["alpha", "beta", "gamma"])
    # This run references only "alpha".
    monkeypatch.setattr(
        docker_module,
        "secret_refs_for_addresses",
        lambda names: {"alpha"},
        raising=False,
    )

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    joined = " ".join(cmd)
    alpha = xdg / "secrets.d" / "alpha.json"
    assert f"{alpha}:/ovid2-user-config/secrets.d/alpha.json:ro" in cmd
    # The other pipelines' secrets are never bound in.
    assert "beta.json" not in joined
    assert "gamma.json" not in joined


@pytest.mark.unit
def test_no_refs_binds_no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no referenced secrets, the tmpfs shadow exposes nothing."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")
    _prepare_xdg_secrets(monkeypatch, tmp_path, ["alpha"])
    monkeypatch.setattr(
        docker_module, "secret_refs_for_addresses", lambda names: set(), raising=False
    )

    instance = OvidInstance(instance_id="20260101-000000-abcdef")
    cmd = docker_module.build_docker_command(_make_args(), instance)
    joined = " ".join(cmd)
    assert "--tmpfs" in cmd
    assert "alpha.json" not in joined


@pytest.mark.unit
def test_addresses_for_args_collects_all_shapes() -> None:
    """ETL and copy/move address attrs are both collected, de-duplicated."""
    from argparse import Namespace

    args = Namespace(
        input_address="in",
        output_address="out",
        from_address="in",  # duplicate — collapsed
        to_address="dest",
    )
    assert docker_module._addresses_for_args(args) == ["in", "out", "dest"]
