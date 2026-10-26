"""Unit tests for docker-network cleanup on a failed cluster bring-up.

These cover the belt-and-suspenders network removal added to
:func:`ovid2.core.docker.start_spark_cluster`: when ``docker compose up``
fails after partially creating the project's network, the instance
network is removed so a failed bring-up never leaks its subnet from
Docker's finite default address pool.
"""

from __future__ import annotations

import subprocess

import pytest

import ovid2.core.docker as docker_mod
from ovid2.core.isolation import OvidInstance


@pytest.mark.unit
def test_remove_instance_network_shells_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_remove_instance_network`` issues ``docker network rm <name>``."""
    calls: list[list[str]] = []

    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run)

    inst = OvidInstance("20260101-000000-abcdef")
    docker_mod._remove_instance_network(inst)

    assert calls == [["docker", "network", "rm", "ovid2-net-20260101-000000-abcdef"]]


@pytest.mark.unit
def test_remove_instance_network_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A docker failure is swallowed (best-effort, reaper is the backstop)."""

    def _boom(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        raise OSError("docker not found")

    monkeypatch.setattr(docker_mod.subprocess, "run", _boom)

    inst = OvidInstance("20260101-000000-abcdef")
    # Must not raise.
    docker_mod._remove_instance_network(inst)


@pytest.mark.unit
def test_start_spark_cluster_removes_network_on_up_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``compose up -d`` triggers removal of the instance network."""
    # Stub the lazy compose-file generation so no pyyaml/FS work happens.
    monkeypatch.setattr(
        "ovid2.core.compose.generate_compose_file",
        lambda *a, **k: None,
    )

    removed: list[str] = []

    def _fake_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        if cmd[:4] == ["docker", "compose", "-f", str(inst.compose_file)]:
            # Simulate ``compose up`` failing (e.g. pool exhausted).
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:3] == ["docker", "network", "rm"]:
            removed.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    inst = OvidInstance("20260101-000000-abcdef")
    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        docker_mod.start_spark_cluster(inst, num_workers=2)

    assert removed == ["ovid2-net-20260101-000000-abcdef"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
