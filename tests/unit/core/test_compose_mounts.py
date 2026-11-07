"""Unit tests for the compose-file volume-mount builder.

Regression guards for the ``LEGACY_CREDENTIALS_DIR`` conditional mount:
before this test existed, the compose generator unconditionally emitted a
``{LEGACY_CREDENTIALS_DIR}:/ovid_credentials:ro`` bind, which docker
rejected at ``up`` time whenever the host lacked the (legacy production)
``/ovid_credentials`` directory. See CHANGELOG entry for the
"OVID2_DATA_DIR / OVID2_CREDENTIALS_DIR fix".
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import ovid2.core.compose as compose_module
from ovid2.core.isolation import OvidInstance


@pytest.fixture
def _instance(monkeypatch: pytest.MonkeyPatch) -> OvidInstance:
    """Build an OvidInstance whose nss-file materialisation is stubbed."""
    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/tmp/passwd"), Path("/tmp/group")),
    )
    # The property paths are read directly by ``_volume_mounts``, so
    # override the ``nss_dir`` too. The two property getters build the
    # final paths from ``nss_dir`` — pointing that at ``/tmp`` makes
    # them ``/tmp/passwd`` / ``/tmp/group`` to match the stub above.
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        inst = OvidInstance(instance_id="20260101-000000-testtt")
    inst.nss_dir = Path("/tmp")
    return inst


@pytest.mark.unit
def test_volume_mounts_includes_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """The mount list always contains DATA_DIR bound at /data:ro."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")

    mounts = compose_module._volume_mounts(instance=_instance)
    joined = "\n".join(mounts)
    assert f"{data_dir}:/data:ro" in joined


@pytest.mark.unit
def test_volume_mounts_includes_credentials_when_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """LEGACY_CREDENTIALS_DIR is mounted when the source directory exists."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", creds_dir)

    mounts = compose_module._volume_mounts(instance=_instance)
    joined = "\n".join(mounts)
    assert f"{creds_dir}:/ovid_credentials:ro" in joined


@pytest.mark.unit
def test_volume_mounts_skips_credentials_when_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """Missing LEGACY_CREDENTIALS_DIR must NOT emit a mount entry.

    Root cause guarded here: docker refuses to bring the compose up when
    a bind mount's source path does not exist; hosts that use
    ``/usr/include/rsa`` (or no legacy credentials at all) would otherwise
    be blocked.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    absent_creds = tmp_path / "absent"  # never created
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", absent_creds)

    mounts = compose_module._volume_mounts(instance=_instance)
    joined = "\n".join(mounts)
    assert "/ovid_credentials" not in joined
    assert str(absent_creds) not in joined


@pytest.mark.unit
def test_volume_mounts_includes_nss_wrapper_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """The nss-wrapper passwd + group overlay files are always mounted.

    Without them, ``docker run --user $UID:$GID`` (which every service
    now uses so mode-0o600 secrets are readable) would leave
    ``getpwuid`` unresolved and every Spark session would crash on
    ``System.getProperty("user.home")``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")

    mounts = compose_module._volume_mounts(instance=_instance)
    joined = "\n".join(mounts)
    assert "/tmp/passwd:/etc/nss_wrapper/passwd:ro" in joined
    assert "/tmp/group:/etc/nss_wrapper/group:ro" in joined


@pytest.mark.unit
def test_volume_mounts_includes_spark_work_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """Per-invocation ``spark_work_dir`` + ``spark_logs_dir`` are bound rw.

    Root cause guarded here: the image's ``$SPARK_HOME/{work,logs}``
    (``/opt/spark/…``) may not be writable for the host uid. With
    ``--user $UID:$GID`` (needed so ``secrets.d/*.json`` mode ``0o600``
    is readable, per AGENTS.md §7), an arbitrary host uid cannot write
    to either baked-in path. Every worker JVM then fails executor
    launch with ``IOException: Failed to create directory
    /opt/spark/work/app-<id>/<executor>/``. After
    ``MAX_EXECUTOR_RETRIES`` (10) failures the master flags the app
    FAILED and the driver crashes mid-``SparkSession.builder.getOrCreate()``
    with the classic ``Master removed our application: FAILED`` +
    ``getServletHandlers on a running MetricsSystem`` +
    ``AppStatusListener NullPointerException`` cascade. Fix:
    bind-mount host-writable dirs at both container paths from
    ``OvidInstance.spark_work_dir`` / ``spark_logs_dir``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")

    mounts = compose_module._volume_mounts(instance=_instance)
    joined = "\n".join(mounts)
    assert f"{_instance.spark_work_dir}:/opt/spark/work:rw" in joined, (
        f"spark_work_dir not bound: {joined!r}"
    )
    assert f"{_instance.spark_logs_dir}:/opt/spark/logs:rw" in joined, (
        f"spark_logs_dir not bound: {joined!r}"
    )


@pytest.mark.unit
def test_compose_mounts_spark_local_dir_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """Every service sees the real-disk shuffle dir; workers export SPARK_LOCAL_DIRS."""
    monkeypatch.setattr(compose_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        compose = compose_module.build_compose_dict(_instance, 2)
    for name, svc in compose["services"].items():
        assert f"{_instance.spark_local_dir}:/ovid2-spark-local:rw" in svc["volumes"], name
    worker = compose["services"][_instance.worker_name(1)]
    assert "SPARK_LOCAL_DIRS=/ovid2-spark-local" in worker["environment"]


@pytest.mark.unit
def test_compose_caps_workers_to_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """25 requested workers on a 96-CPU / 958 GiB host -> 11 services."""
    monkeypatch.setattr(compose_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")
    monkeypatch.setenv("OVID2_HOST_CPUS", "96")
    monkeypatch.setenv("OVID2_HOST_MEMORY_GB", "958")
    monkeypatch.delenv("OVID2_SPARK_ALLOW_OVERSUBSCRIBE", raising=False)
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        compose = compose_module.build_compose_dict(_instance, 25)
    workers = [k for k in compose["services"] if k.startswith("ovid2-worker-")]
    assert len(workers) == 11


@pytest.mark.unit
def test_compose_oversubscribe_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    monkeypatch.setattr(compose_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(compose_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "nope")
    monkeypatch.setenv("OVID2_HOST_CPUS", "96")
    monkeypatch.setenv("OVID2_HOST_MEMORY_GB", "958")
    monkeypatch.setenv("OVID2_SPARK_ALLOW_OVERSUBSCRIBE", "1")
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        compose = compose_module.build_compose_dict(_instance, 25)
    workers = [k for k in compose["services"] if k.startswith("ovid2-worker-")]
    assert len(workers) == 25


@pytest.mark.unit
def test_volume_mounts_include_work_dir_at_identical_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _instance: OvidInstance
) -> None:
    """Executors write ``legacy_ovid`` stage-boundary scratch under WORK_DIR.

    Guards ``Mkdirs failed to create file:/run/user/<uid>/ovid2/legacy_rt_*``
    on cluster workers (the driver container already mounted WORK_DIR).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    work = tmp_path / "work"
    monkeypatch.setattr(compose_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(compose_module, "WORK_DIR", work)
    mounts = compose_module._volume_mounts(instance=_instance)
    assert f"{work}:{work}:rw" in mounts
    assert work.is_dir()
