"""Unit tests for the nss-wrapper overlay used to run containers as host uid.

The Ovid2 image runs ``USER 1001`` by default, but the mounted XDG
secrets tree (``$XDG_CONFIG_HOME/ovid2/secrets.d/*.json``) is mode
``0o600`` owned by the invoking host user. To read those secrets the
container must run as ``--user $UID:$GID``, which in turn leaves
``getpwuid`` unresolvable inside the container — Java would then see
``user.home = "?"`` and every ``SparkSession.builder.getOrCreate()``
would trip ``java.lang.IllegalArgumentException: basedir must be
absolute: ?/.ivy2/local``.

The fix is a per-invocation ``passwd`` / ``group`` overlay served via
``libnss_wrapper.so`` (bundled inside the Bitnami base image at
``/opt/bitnami/common/lib/``). This test file locks the surface:

- :meth:`OvidInstance.ensure_nss_files` writes the files.
- :func:`ovid2.core.docker.nss_wrapper_env` returns the ``docker run``
  flags that activate the overlay.
- :func:`ovid2.core.docker.build_docker_command` splats ``--user`` and
  the overlay flags into every generated argv.
- :func:`ovid2.core.compose.build_compose_dict` sets ``user:`` and
  ``LD_PRELOAD=libnss_wrapper.so`` on master + every worker.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

from ovid2.core.compose import build_compose_dict
from ovid2.core.docker import (
    NSS_GROUP_IN_CONTAINER,
    NSS_PASSWD_IN_CONTAINER,
    NSS_WRAPPER_LIB,
    build_docker_command,
    nss_wrapper_env,
)
from ovid2.core.isolation import OvidInstance

# ---------------------------------------------------------------------------
# OvidInstance.ensure_nss_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ensure_nss_files_writes_passwd_and_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The method writes both overlay files under ``nss_dir``."""
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    import importlib

    from ovid2.config import settings as settings_module

    importlib.reload(settings_module)
    from ovid2.core import isolation as isolation_module

    importlib.reload(isolation_module)
    inst = isolation_module.OvidInstance(instance_id="20260101-000000-abcdef")
    passwd_path, group_path = inst.ensure_nss_files(uid=99999, gid=88888)

    assert passwd_path.is_file()
    assert group_path.is_file()
    assert passwd_path.parent == inst.nss_dir
    assert group_path.parent == inst.nss_dir

    passwd = passwd_path.read_text(encoding="utf-8")
    assert "99999:88888" in passwd
    assert ":/docker_data/spark_tmp:" in passwd, (
        "HOME must land on /docker_data/spark_tmp (chmod-777 in Dockerfile)."
    )

    group = group_path.read_text(encoding="utf-8")
    assert "88888" in group


@pytest.mark.unit
def test_ensure_nss_files_defaults_to_current_uid_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When called without args, the method uses the process's uid/gid."""
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    import importlib

    from ovid2.config import settings as settings_module

    importlib.reload(settings_module)
    from ovid2.core import isolation as isolation_module

    importlib.reload(isolation_module)
    import os

    inst = isolation_module.OvidInstance(instance_id="20260101-000000-abcdef")
    passwd_path, _ = inst.ensure_nss_files()

    passwd = passwd_path.read_text(encoding="utf-8")
    assert f":{os.getuid()}:{os.getgid()}:" in passwd


@pytest.mark.unit
def test_ensure_nss_files_permissions_are_world_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay files carry no secret material and must be readable.

    They contain only uid/gid/home/name pairs — no credentials. Making
    them ``0o644`` lets any uid inside the container consume them
    without a chmod dance.
    """
    import stat

    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    import importlib

    from ovid2.config import settings as settings_module

    importlib.reload(settings_module)
    from ovid2.core import isolation as isolation_module

    importlib.reload(isolation_module)
    inst = isolation_module.OvidInstance(instance_id="20260101-000000-abcdef")
    passwd_path, group_path = inst.ensure_nss_files()

    for path in (passwd_path, group_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o644, f"{path} is {oct(mode)}, expected 0o644"


@pytest.mark.unit
def test_cleanup_removes_nss_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``OvidInstance.cleanup()`` scrubs the nss-wrapper overlay tree."""
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    import importlib

    from ovid2.config import settings as settings_module

    importlib.reload(settings_module)
    from ovid2.core import isolation as isolation_module

    importlib.reload(isolation_module)
    inst = isolation_module.OvidInstance(instance_id="20260101-000000-abcdef")
    inst.ensure_nss_files()
    assert inst.nss_dir.is_dir()

    inst.cleanup()
    assert not inst.nss_dir.exists()


# ---------------------------------------------------------------------------
# nss_wrapper_env
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nss_wrapper_env_returns_expected_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper returns the ``LD_PRELOAD`` + passwd/group mount pair."""
    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/host/passwd"), Path("/host/group")),
    )
    inst = OvidInstance(instance_id="20260101-000000-abcdef")
    env_flags, mount_flags = nss_wrapper_env(inst)

    joined = " ".join(env_flags)
    assert f"LD_PRELOAD={NSS_WRAPPER_LIB}" in joined
    assert f"NSS_WRAPPER_PASSWD={NSS_PASSWD_IN_CONTAINER}" in joined
    assert f"NSS_WRAPPER_GROUP={NSS_GROUP_IN_CONTAINER}" in joined

    joined = " ".join(mount_flags)
    assert f"/host/passwd:{NSS_PASSWD_IN_CONTAINER}:ro" in joined
    assert f"/host/group:{NSS_GROUP_IN_CONTAINER}:ro" in joined


# ---------------------------------------------------------------------------
# build_docker_command
# ---------------------------------------------------------------------------


def _mk_args() -> Namespace:
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


@pytest.fixture
def _stubbed_instance(monkeypatch: pytest.MonkeyPatch) -> OvidInstance:
    """Return an OvidInstance whose nss overlay materialisation is a no-op."""
    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/host/passwd"), Path("/host/group")),
    )
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        return OvidInstance(instance_id="20260101-000000-abcdef")


@pytest.mark.unit
def test_build_docker_command_emits_user_flag(
    _stubbed_instance: OvidInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docker run --user <uid>:<gid>`` is set to the host uid/gid."""
    monkeypatch.setattr("ovid2.core.docker._host_user", lambda: (12345, 67890))
    cmd = build_docker_command(_mk_args(), _stubbed_instance)
    idx = cmd.index("--user")
    assert cmd[idx + 1] == "12345:67890"


@pytest.mark.unit
def test_build_docker_command_emits_nss_env_and_mounts(
    _stubbed_instance: OvidInstance,
) -> None:
    """The nss overlay LD_PRELOAD + bind mounts land on the argv."""
    cmd = build_docker_command(_mk_args(), _stubbed_instance)
    joined = " ".join(cmd)
    assert f"LD_PRELOAD={NSS_WRAPPER_LIB}" in joined
    assert f"/host/passwd:{NSS_PASSWD_IN_CONTAINER}:ro" in joined
    assert f"/host/group:{NSS_GROUP_IN_CONTAINER}:ro" in joined


# ---------------------------------------------------------------------------
# build_compose_dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_compose_dict_sets_user_on_every_service(
    _stubbed_instance: OvidInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Master + every worker service carries ``user: <uid>:<gid>``."""
    _stubbed_instance.nss_dir = Path("/host")
    monkeypatch.setattr("os.getuid", lambda: 12345)
    monkeypatch.setattr("os.getgid", lambda: 67890)
    compose = build_compose_dict(_stubbed_instance, num_workers=3)
    assert len(compose["services"]) == 4
    for name, svc in compose["services"].items():
        assert svc.get("user") == "12345:67890", (
            f"service {name} missing user: field ({svc.get('user')!r})"
        )


@pytest.mark.unit
def test_build_compose_dict_sets_nss_env_on_every_service(
    _stubbed_instance: OvidInstance,
) -> None:
    """Master + every worker gets ``LD_PRELOAD=libnss_wrapper.so`` set."""
    _stubbed_instance.nss_dir = Path("/host")
    compose = build_compose_dict(_stubbed_instance, num_workers=2)
    for name, svc in compose["services"].items():
        env_list = svc["environment"]
        joined = " ".join(env_list)
        assert f"LD_PRELOAD={NSS_WRAPPER_LIB}" in joined, (
            f"service {name} missing LD_PRELOAD ({env_list})"
        )
        assert f"NSS_WRAPPER_PASSWD={NSS_PASSWD_IN_CONTAINER}" in joined
        assert f"NSS_WRAPPER_GROUP={NSS_GROUP_IN_CONTAINER}" in joined


@pytest.mark.unit
def test_build_compose_dict_mounts_nss_overlay_on_every_service(
    _stubbed_instance: OvidInstance,
) -> None:
    """The passwd + group overlay files are bind-mounted uniformly."""
    _stubbed_instance.nss_dir = Path("/host")
    compose = build_compose_dict(_stubbed_instance, num_workers=2)
    for name, svc in compose["services"].items():
        joined = "\n".join(svc["volumes"])
        assert f"/host/passwd:{NSS_PASSWD_IN_CONTAINER}:ro" in joined, (
            f"service {name} missing passwd overlay mount"
        )
        assert f"/host/group:{NSS_GROUP_IN_CONTAINER}:ro" in joined, (
            f"service {name} missing group overlay mount"
        )


@pytest.mark.unit
def test_master_publishes_ephemeral_ports_only(
    _stubbed_instance: OvidInstance,
) -> None:
    """The master must not bind *fixed* host ports (breaks isolation).

    Fixed host bindings (``"8080:8080"`` / ``"7077:7077"`` / ``"4040:4040"``)
    let a second — or leaked/cancelled — ovid2 cluster collide with
    ``Bind for 0.0.0.0:<port> failed: port is already allocated``. The
    ephemeral, single-value form (``"8080"``) lets Docker pick a free
    host port, preserving per-invocation isolation. The 4040 driver UI
    is not published on the master at all.
    """
    _stubbed_instance.nss_dir = Path("/host")
    compose = build_compose_dict(_stubbed_instance, num_workers=1)
    master = compose["services"][_stubbed_instance.master_name]
    ports = master.get("ports", [])
    # No entry may pin a host-side port (contains ':' with a left side).
    for mapping in ports:
        assert ":" not in str(mapping), (
            f"master pins a fixed host port {mapping!r}; use the "
            f"ephemeral single-value form to preserve isolation"
        )
    # The container ports we still expose for optional UI/RPC browsing.
    assert "8080" in ports  # Spark master web UI
    assert "7077" in ports  # Spark master RPC
    # 4040 (driver application UI) belongs on the driver, not the master.
    assert "4040" not in ports
    # Workers publish nothing to the host.
    for i in range(1, 2):
        worker = compose["services"][_stubbed_instance.worker_name(i)]
        assert "ports" not in worker
