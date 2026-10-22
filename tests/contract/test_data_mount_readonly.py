"""Contract: ``/data`` is mounted read-only for every ETL-family command.

The ETL pipeline (etl / analysis / dq_checks) reads from ``/data/inbox``
and writes to Snowflake, disc-output-under-a-different-root, or another
externally-provisioned sink — never back to ``/data``. Mounting the
tree read-only prevents a mis-authored formatter or fixer from
silently mutating an input drop and eroding the "inputs are immutable"
guarantee the pipeline relies on for reproducibility.

The ``copy`` kind is the sole exception. It legitimately writes to
whatever the destination address points at, which for disc-family
destinations is under ``/data``. The copy kind explicitly bypasses
formatter/mapper/fixer stages (see
the adr-copy-job-kind design note), so the read-only
contract has nothing to protect there.

This test locks the invariant at both mount surfaces:

- :func:`ovid2.core.docker.build_docker_command` (SPARK_MODE=local)
- :func:`ovid2.core.compose.build_compose_dict` (SPARK_MODE=cluster)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest import mock

import pytest

from ovid2.core.compose import build_compose_dict
from ovid2.core.docker import build_docker_command
from ovid2.core.isolation import OvidInstance


@pytest.fixture(autouse=True)
def _stub_nss_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``build_compose_dict`` / ``build_docker_command`` from touching disk.

    Both call :meth:`OvidInstance.ensure_nss_files` (to generate the
    per-invocation ``passwd`` / ``group`` overlay that lets containers
    run as the host uid) and :meth:`OvidInstance.ensure_directories`
    (to materialise the per-invocation Spark work / logs dirs bound at
    ``/opt/bitnami/spark/{work,logs}``). Stubbing both here keeps this
    mount-contract test hermetic.
    """
    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/tmp/passwd"), Path("/tmp/group")),
    )
    monkeypatch.setattr(
        OvidInstance,
        "ensure_directories",
        lambda self: None,
    )


def _mk_args(command: str) -> argparse.Namespace:
    """Build a minimal argparse namespace for ``build_docker_command``."""
    return argparse.Namespace(
        command=command,
        input_address="src",
        output_address="dst",
        pipeline="p",
        batch_id="20260101",
        tables=["demographic"],
        transformations=[],
        engine="pyspark",
        workers=25,
        sf_database=None,
        sf_account=None,
        sf_schema=None,
        from_address="src",
        to_address="dst",
    )


def _find_data_mount(cmd: list[str]) -> str:
    """Return the ``-v host:/data:MODE`` value from a docker run argv."""
    for i, tok in enumerate(cmd):
        if tok == "-v" and i + 1 < len(cmd) and ":/data:" in cmd[i + 1]:
            return cmd[i + 1]
    raise AssertionError(f"no /data mount found in argv: {cmd}")


# ---------------------------------------------------------------------------
# docker.py (local Spark, SPARK_MODE != "cluster")
# ---------------------------------------------------------------------------


class TestDockerRunDataMount:
    """``build_docker_command`` picks the mount mode from ``args.command``."""

    def test_etl_mounts_data_readonly(self) -> None:
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(_mk_args("etl"), _instance())
        assert _find_data_mount(cmd).endswith(":/data:ro"), \
            "etl must never mount /data rw — inputs are immutable"

    def test_analysis_mounts_data_readonly(self) -> None:
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(_mk_args("analysis"), _instance())
        assert _find_data_mount(cmd).endswith(":/data:ro")

    def test_dq_checks_mounts_data_readonly(self) -> None:
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(_mk_args("dq_checks"), _instance())
        assert _find_data_mount(cmd).endswith(":/data:ro")

    def test_tools_copy_mounts_data_readwrite(self) -> None:
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(_mk_args("tools copy"), _instance())
        assert _find_data_mount(cmd).endswith(":/data:rw"), \
            "kind=copy must mount /data rw so its destination address can write"

    def test_missing_command_defaults_to_readonly(self) -> None:
        """A namespace without ``command`` is treated as the etl shape."""
        args = _mk_args("etl")
        del args.command
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(args, _instance())
        assert _find_data_mount(cmd).endswith(":/data:ro"), \
            "unknown/missing command must never widen to rw"


# ---------------------------------------------------------------------------
# compose.py (cluster mode)
# ---------------------------------------------------------------------------


class TestComposeDataMount:
    """``build_compose_dict`` respects the same rw-only-for-copy rule."""

    def test_default_is_readonly(self) -> None:
        compose = build_compose_dict(_instance(), num_workers=2)
        for svc in compose["services"].values():
            data_mounts = [v for v in svc["volumes"] if ":/data:" in v]
            assert data_mounts, f"service {svc} missing /data mount"
            assert all(v.endswith(":/data:ro") for v in data_mounts), \
                f"cluster ETL must not mount /data rw: {data_mounts}"

    def test_data_rw_flag_widens_every_service(self) -> None:
        compose = build_compose_dict(_instance(), num_workers=2, data_rw=True)
        # master + N workers must all get rw — a single ro worker
        # would silently break writes for whichever partition landed
        # on it, so the flag applies uniformly.
        for name, svc in compose["services"].items():
            data_mounts = [v for v in svc["volumes"] if ":/data:" in v]
            assert data_mounts, f"service {name} missing /data mount"
            assert all(v.endswith(":/data:rw") for v in data_mounts), \
                f"service {name} must mount /data rw when data_rw=True: {data_mounts}"

    def test_extra_mounts_appear_on_every_service(self, tmp_path) -> None:
        """A single missing worker mount silently strands whichever
        partition landed on that node; the flag must be uniform."""
        from ovid2.tools.extra_mounts import ExtraMount

        em = ExtraMount(host=str(tmp_path), container="/copy_out", mode="rw")
        compose = build_compose_dict(
            _instance(), num_workers=3, data_rw=True, extra_mounts=[em],
        )
        assert len(compose["services"]) == 4  # 1 master + 3 workers
        for name, svc in compose["services"].items():
            hit = [v for v in svc["volumes"] if "/copy_out" in v]
            assert hit == [f"{tmp_path}:/copy_out:rw"], \
                f"extra mount missing / wrong on {name}: {svc['volumes']}"

    def test_extra_mounts_default_none_leaves_volumes_unchanged(self) -> None:
        """The extra_mounts kwarg is optional and non-invasive by default."""
        base = build_compose_dict(_instance(), num_workers=1)
        with_none = build_compose_dict(_instance(), num_workers=1, extra_mounts=None)
        with_empty = build_compose_dict(_instance(), num_workers=1, extra_mounts=[])
        for svc_name in base["services"]:
            assert base["services"][svc_name]["volumes"] == \
                   with_none["services"][svc_name]["volumes"] == \
                   with_empty["services"][svc_name]["volumes"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _instance() -> OvidInstance:
    """Build an OvidInstance without touching the filesystem."""
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        return OvidInstance(instance_id="20260101-000000-testtt")
