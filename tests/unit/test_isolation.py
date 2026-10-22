"""Unit tests for :class:`ovid2.core.isolation.OvidInstance`."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from ovid2.core.isolation import OvidInstance, _generate_instance_id


@pytest.mark.unit
def test_instance_id_format() -> None:
    """Instance IDs must match ``YYYYMMDD-HHMMSS-{6hex}``."""
    instance_id = _generate_instance_id()
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", instance_id)


@pytest.mark.unit
def test_instance_derived_names_use_instance_id() -> None:
    """Every derived name embeds the instance id."""
    inst = OvidInstance("20260707-101112-abc123")
    assert inst.project_name == "ovid2-20260707-101112-abc123"
    assert inst.master_name == "ovid2-master-20260707-101112-abc123"
    assert inst.network_name == "ovid2-net-20260707-101112-abc123"
    assert inst.worker_name(3) == "ovid2-worker-3-20260707-101112-abc123"
    assert inst.spark_master_url() == "spark://ovid2-master-20260707-101112-abc123:7077"
    assert inst.compose_file.name == "docker-compose-20260707-101112-abc123.yml"
    assert inst.spark_tmp_dir.name == "spark_tmp-20260707-101112-abc123"
    assert inst.spark_work_dir.name == "spark_work-20260707-101112-abc123"
    assert inst.spark_logs_dir.name == "spark_logs-20260707-101112-abc123"
    assert inst.event_log.name == "events-20260707-101112-abc123.jsonl"


@pytest.mark.unit
def test_instance_repr_includes_id() -> None:
    """``repr()`` mentions the id for debuggability."""
    inst = OvidInstance("test-instance")
    assert "test-instance" in repr(inst)


@pytest.mark.unit
def test_instance_honors_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``OVID2_INSTANCE_ID`` is set (and valid), it wins over auto-gen."""
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20260101-100000-abcdef")
    inst = OvidInstance()
    assert inst.instance_id == "20260101-100000-abcdef"


@pytest.mark.unit
def test_instance_rejects_bad_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed ``OVID2_INSTANCE_ID`` is ignored — a fresh id is generated."""
    monkeypatch.setenv("OVID2_INSTANCE_ID", "not; a valid id")
    inst = OvidInstance()
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", inst.instance_id)
    assert inst.instance_id != "not; a valid id"


@pytest.mark.unit
def test_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit constructor arg takes precedence over the env var."""
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20260101-100000-abcdef")
    inst = OvidInstance("20260202-200000-ffffff")
    assert inst.instance_id == "20260202-200000-ffffff"


@pytest.mark.unit
def test_ensure_directories_creates_spark_work_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ensure_directories`` creates the per-invocation Spark work/logs dirs.

    Root cause guarded here: the Bitnami base image ships
    ``/opt/bitnami/spark/{work,logs}`` as ``0775 ovid2:root``. With
    ``--user $UID:$GID``, an arbitrary host uid cannot write to
    either, and every worker executor launch dies with
    ``IOException: Failed to create directory
    /opt/bitnami/spark/work/app-<id>/<executor>/``. The master then
    marks the app FAILED after 10 retries and the driver crashes
    mid-``getOrCreate()``. Fix: bind-mount host-owned ``0o777`` dirs
    onto both container paths. This test guards the host-side dir
    creation.
    """
    import ovid2.core.isolation as iso

    monkeypatch.setattr(iso, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(iso, "DOCKER_DATA_DIR", tmp_path / "docker_data")

    inst = OvidInstance("20260101-000000-testct")
    # Re-derive paths after the monkeypatch. (Instances cache paths at
    # construction time from the module constants; construct AFTER the
    # patches so the paths land under ``tmp_path``.)
    inst.spark_tmp_dir = tmp_path / "docker_data" / f"spark_tmp-{inst.instance_id}"
    inst.spark_work_dir = tmp_path / "docker_data" / f"spark_work-{inst.instance_id}"
    inst.spark_logs_dir = tmp_path / "docker_data" / f"spark_logs-{inst.instance_id}"

    inst.ensure_directories()

    assert inst.spark_work_dir.is_dir()
    assert inst.spark_logs_dir.is_dir()
    # ``0o777`` so worker JVMs (any host uid) can write freely.
    work_mode = inst.spark_work_dir.stat().st_mode & 0o777
    logs_mode = inst.spark_logs_dir.stat().st_mode & 0o777
    assert work_mode == 0o777, f"spark_work_dir mode is {oct(work_mode)}"
    assert logs_mode == 0o777, f"spark_logs_dir mode is {oct(logs_mode)}"


@pytest.mark.unit
def test_cleanup_removes_spark_work_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cleanup`` reaps the per-invocation Spark work/logs dirs."""
    import ovid2.core.isolation as iso

    monkeypatch.setattr(iso, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(iso, "DOCKER_DATA_DIR", tmp_path / "docker_data")

    inst = OvidInstance("20260101-000000-testct")
    inst.spark_tmp_dir = tmp_path / "docker_data" / f"spark_tmp-{inst.instance_id}"
    inst.spark_work_dir = tmp_path / "docker_data" / f"spark_work-{inst.instance_id}"
    inst.spark_logs_dir = tmp_path / "docker_data" / f"spark_logs-{inst.instance_id}"

    inst.ensure_directories()
    # Drop a stub file so ``rmtree`` has something to reap.
    (inst.spark_work_dir / "app-x").mkdir()
    (inst.spark_logs_dir / "worker.out").write_text("log line\n", encoding="utf-8")

    inst.cleanup()

    assert not inst.spark_work_dir.exists()
    assert not inst.spark_logs_dir.exists()
    assert not inst.spark_tmp_dir.exists()


@pytest.mark.unit
def test_cleanup_stale_reaps_spark_work_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cleanup_stale_instances`` sweeps orphaned work/logs dirs."""
    import ovid2.core.isolation as iso

    work_root = tmp_path / "work"
    data_root = tmp_path / "docker_data"
    work_root.mkdir()
    data_root.mkdir()
    monkeypatch.setattr(iso, "WORK_DIR", work_root)
    monkeypatch.setattr(iso, "DOCKER_DATA_DIR", data_root)
    # Isolate from a real docker daemon: the network reaper is exercised
    # by its own tests below.
    monkeypatch.setattr(iso, "_reap_orphaned_networks", lambda *a, **k: None)

    stale_work = data_root / "spark_work-20250101-000000-deadbe"
    stale_logs = data_root / "spark_logs-20250101-000000-deadbe"
    stale_work.mkdir()
    stale_logs.mkdir()
    # Force mtime way in the past so the age check trips.
    old = 1_000_000
    os.utime(stale_work, (old, old))
    os.utime(stale_logs, (old, old))

    iso.cleanup_stale_instances(max_age_hours=1)

    assert not stale_work.exists()
    assert not stale_logs.exists()


def _make_fake_docker(
    networks: dict[str, tuple[int, str]],
    removed: list[str],
) -> object:
    """Return a fake ``subprocess.run`` that emulates the docker CLI.

    Args:
        networks: Map of ``ovid2-net-*`` name → ``(container_count,
            created_iso)`` describing the daemon's current state.
        removed: A list the fake appends removed network names to, so a
            test can assert exactly which networks were reaped.

    The fake understands three argv shapes: ``network ls`` (returns the
    names), ``network inspect`` (returns ``count|created``), and
    ``network rm`` (records the removal).
    """
    import subprocess as _sp

    def _fake_run(cmd: list[str], *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        # docker network ls --filter name=^ovid2-net- --format {{.Name}}
        if cmd[:3] == ["docker", "network", "ls"]:
            names = "\n".join(networks.keys())
            return _sp.CompletedProcess(cmd, 0, stdout=names + "\n", stderr="")
        # docker network inspect <name> --format {{len .Containers}}|{{.Created}}
        if cmd[:3] == ["docker", "network", "inspect"]:
            name = cmd[3]
            if name not in networks:
                return _sp.CompletedProcess(cmd, 1, stdout="", stderr="no such network")
            count, created = networks[name]
            return _sp.CompletedProcess(cmd, 0, stdout=f"{count}|{created}\n", stderr="")
        # docker network rm <name>
        if cmd[:3] == ["docker", "network", "rm"]:
            removed.append(cmd[3])
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker command: {cmd!r}")

    return _fake_run


@pytest.mark.unit
def test_reap_orphaned_networks_removes_only_empty_and_aged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only 0-container, older-than-threshold ``ovid2-net-*`` are removed."""
    import ovid2.core.isolation as iso

    now = 1_000_000.0
    hour = 3600.0
    old_iso = _iso_ts(now - 10 * hour)  # 10h old → aged
    young_iso = _iso_ts(now - 1 * hour)  # 1h old → too young

    networks = {
        "ovid2-net-20260101-000000-aaaaaa": (0, old_iso),  # remove
        "ovid2-net-20260101-000000-bbbbbb": (26, old_iso),  # in use → keep
        "ovid2-net-20260101-000000-cccccc": (0, young_iso),  # too young → keep
    }
    removed: list[str] = []
    monkeypatch.setattr(iso.subprocess, "run", _make_fake_docker(networks, removed))

    iso._reap_orphaned_networks(max_age_hours=6, now=now)

    assert removed == ["ovid2-net-20260101-000000-aaaaaa"]


@pytest.mark.unit
def test_reap_orphaned_networks_removes_unparseable_age_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty network with an unparseable ``Created`` is still reaped."""
    import ovid2.core.isolation as iso

    networks = {
        "ovid2-net-20260101-000000-dddddd": (0, "not-a-timestamp"),
    }
    removed: list[str] = []
    monkeypatch.setattr(iso.subprocess, "run", _make_fake_docker(networks, removed))

    iso._reap_orphaned_networks(max_age_hours=6, now=1_000_000.0)

    assert removed == ["ovid2-net-20260101-000000-dddddd"]


@pytest.mark.unit
def test_reap_orphaned_networks_survives_ls_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``docker network ls`` is swallowed (best-effort)."""
    import subprocess as _sp

    import ovid2.core.isolation as iso

    def _boom(cmd: list[str], *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if cmd[:3] == ["docker", "network", "ls"]:
            return _sp.CompletedProcess(cmd, 1, stdout="", stderr="daemon down")
        raise AssertionError("should not inspect/rm when ls fails")

    monkeypatch.setattr(iso.subprocess, "run", _boom)

    # Must not raise.
    iso._reap_orphaned_networks(max_age_hours=6, now=1_000_000.0)


def _iso_ts(epoch: float) -> str:
    """Return an RFC3339 timestamp (with tz offset) for a POSIX time."""
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().isoformat()
