"""Unit tests for ``ovid2 dev`` (per-developer isolated instances)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from pathlib import Path

import pytest

from ovid2.cli import dev


@pytest.fixture()
def dev_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the instance home and lease registry at ``tmp_path``."""
    monkeypatch.setenv("OVID2_DEV_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OVID2_DEV_REGISTRY", str(tmp_path / "registry"))
    monkeypatch.setenv("OVID2_DEV_PORT_RANGE", "18100-18119")
    monkeypatch.setenv("OVID2_DEV_HOST", "127.0.0.1")
    return tmp_path


def _free_even_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.unit
def test_validate_name_rejects_path_escape() -> None:
    assert dev.validate_name("feature-x_1") == "feature-x_1"
    for bad in ("", "../x", "A", "a/b", "x" * 40):
        with pytest.raises(ValueError, match="invalid instance name"):
            dev.validate_name(bad)


@pytest.mark.unit
def test_port_range_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVID2_DEV_PORT_RANGE", raising=False)
    assert dev.port_range() == dev.DEFAULT_PORT_RANGE
    monkeypatch.setenv("OVID2_DEV_PORT_RANGE", "9000-9010")
    assert dev.port_range() == (9000, 9010)
    monkeypatch.setenv("OVID2_DEV_PORT_RANGE", "9000")
    with pytest.raises(ValueError):
        dev.port_range()


@pytest.mark.unit
def test_default_host_honours_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_DEV_HOST", "127.0.0.9")
    assert dev.default_host() == "127.0.0.9"
    monkeypatch.delenv("OVID2_DEV_HOST")
    assert dev.default_host()  # discovered or loopback; never empty


@pytest.mark.unit
def test_instance_env_isolates_and_strips_leaky_vars(tmp_path: Path) -> None:
    base = {
        "PATH": "/usr/bin",
        "OVID2_SCHEDULER_DB": "/elsewhere/scheduler.db",
        "OVID2_CONNECTIONS_DIR": "/elsewhere/connections",
        "XDG_CONFIG_HOME": "/home/someone/.config",
        "OVID2_DATA_DIR": "/shared/data",
    }
    env = dev.instance_env(
        tmp_path,
        name="a",
        host="127.0.0.1",
        port=8100,
        mcp_port=8101,
        image="ovid2:dev-x",
        base=base,
    )
    assert env["PATH"] == "/usr/bin"
    assert "OVID2_SCHEDULER_DB" not in env
    assert "OVID2_CONNECTIONS_DIR" not in env
    assert env["OVID2_DATA_DIR"] == "/shared/data"  # shared input data is intentional
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
    assert env["OVID2_WEB_BIND"] == "127.0.0.1:8100"
    assert env["OVID2_MCP_BIND"] == "127.0.0.1:8101"
    assert env["OVID2_WEB_URL"] == "http://127.0.0.1:8100"
    assert env["OVID2_DOCKER_IMAGE"] == "ovid2:dev-x"
    for key in ("OVID2_WORK_DIR", "OVID2_ARCHIVE_DIR", "OVID2_ACCESS_MIRROR_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert base["XDG_CONFIG_HOME"] == "/home/someone/.config"  # input untouched


@pytest.mark.unit
def test_cookie_name_unique_per_instance() -> None:
    assert dev.cookie_name("a") != dev.cookie_name("b")
    assert dev.cookie_name("a").startswith("ovid2_session_")
    assert all(c.isalnum() or c == "_" for c in dev.cookie_name("feat-x"))


@pytest.mark.unit
def test_default_image_is_per_user_not_shared() -> None:
    assert dev.default_image().startswith("ovid2:dev-")
    assert dev.default_image() != "ovid2:latest"


@pytest.mark.unit
def test_lease_pairs_are_distinct_and_released(dev_env: Path) -> None:
    root = dev_env / "home" / "x"
    first = dev.acquire_lease(name="a", root=root, host="127.0.0.1")
    second = dev.acquire_lease(name="b", root=root, host="127.0.0.1")
    assert first.port % 2 == 0 and first.mcp_port == first.port + 1
    assert {first.port, first.mcp_port}.isdisjoint({second.port, second.mcp_port})
    assert {lease.name for lease in dev.list_leases()} == {"a", "b"}
    dev.release_lease(first.port)
    assert [lease.name for lease in dev.list_leases()] == ["b"]
    mode = (dev_env / "registry" / "ports").stat().st_mode
    assert mode & 0o1777 == 0o1777


@pytest.mark.unit
def test_concurrent_claims_never_collide(dev_env: Path) -> None:
    results: list[int] = []
    errors: list[Exception] = []

    def claim(i: int) -> None:
        try:
            results.append(dev.acquire_lease(name=f"n{i}", root=dev_env, host="127.0.0.1").port)
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == len(set(results)) == 8
    assert not errors


@pytest.mark.unit
def test_range_exhaustion_raises(dev_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_DEV_PORT_RANGE", "18100-18103")
    dev.acquire_lease(name="a", root=dev_env, host="127.0.0.1")
    dev.acquire_lease(name="b", root=dev_env, host="127.0.0.1")
    with pytest.raises(RuntimeError, match="no free port pair"):
        dev.acquire_lease(name="c", root=dev_env, host="127.0.0.1")


@pytest.mark.unit
def test_stale_lease_is_reclaimed(dev_env: Path) -> None:
    lease = dev.acquire_lease(name="a", root=dev_env, host="127.0.0.1", port=18110)
    lease.pids = [2**22 + 12345]  # not a live pid
    dev.update_lease(lease)
    again = dev.acquire_lease(name="b", root=dev_env, host="127.0.0.1", port=18110)
    assert again.name == "b"


@pytest.mark.unit
def test_live_lease_blocks_explicit_port(dev_env: Path) -> None:
    lease = dev.acquire_lease(name="a", root=dev_env, host="127.0.0.1", port=18112)
    lease.pids = [os.getpid()]
    dev.update_lease(lease)
    with pytest.raises(RuntimeError, match="in use or leased"):
        dev.acquire_lease(name="b", root=dev_env, host="127.0.0.1", port=18112)


@pytest.mark.unit
def test_port_in_use_by_other_process_is_skipped(dev_env: Path) -> None:
    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 18100))
        busy.listen()
        lease = dev.acquire_lease(name="a", root=dev_env, host="127.0.0.1")
    assert lease.port != 18100


def _up_args(name: str, **kw: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "name": name,
        "port": None,
        "host": None,
        "image": "ovid2:test",
        "no_worker": False,
        "mcp": False,
        "timeout": 5.0,
        "purge": False,
        "yes": True,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


@pytest.mark.unit
def test_up_down_lifecycle_with_stubbed_processes(
    dev_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spawned: list[tuple[list[str], dict[str, str]]] = []

    def fake_spawn(argv: list[str], *, root: Path, env: dict[str, str], log: Path) -> int:
        spawned.append((argv, dict(env)))
        assert root == dev_env / "home" / "a"
        return os.getpid()

    monkeypatch.setattr(dev, "_spawn", fake_spawn)
    monkeypatch.setattr(dev, "_wait_listening", lambda *a, **k: True)
    monkeypatch.setattr(dev, "_ensure_image", lambda image: None)
    monkeypatch.setattr(dev, "_is_ovid2_process", lambda pid: True)
    stopped: list[dict[str, int]] = []
    monkeypatch.setattr(dev, "_stop_pids", lambda pids, timeout=40.0: stopped.append(dict(pids)))

    assert dev._cmd_up(_up_args("a")) == 0
    root = dev_env / "home" / "a"
    meta = json.loads((root / "instance.json").read_text())
    assert set(meta["pids"]) == {"web", "worker"}
    assert (root / "config" / "ovid2" / "configs").is_dir()  # ovid2 init ran in the instance
    settings = json.loads((root / "config" / "ovid2" / "settings.json").read_text())
    assert settings["auth"]["cookie_name"] == dev.cookie_name("a")
    web_argv, web_env = spawned[0]
    assert web_argv[-2:] == ["--bind", f"127.0.0.1:{meta['port']}"]
    assert web_env["XDG_STATE_HOME"] == str(root / "state")
    assert "is up" in capsys.readouterr().out

    # Second `up` is a no-op while running.
    assert dev._cmd_up(_up_args("a")) == 0
    assert len(spawned) == 2
    assert "already running" in capsys.readouterr().out

    assert dev._cmd_down(_up_args("a")) == 0
    assert stopped and set(stopped[0]) == {"web", "worker"}
    assert dev.list_leases() == []

    assert dev._cmd_down(_up_args("a", purge=True)) == 0
    assert not root.exists()


@pytest.mark.unit
def test_env_prints_exports(dev_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = dev.instance_root("a")
    root.mkdir(parents=True)
    dev.write_meta(
        root, {"host": "127.0.0.1", "port": 18100, "mcp_port": 18101, "image": "ovid2:t"}
    )
    assert dev._cmd_env(argparse.Namespace(name="a")) == 0
    out = capsys.readouterr().out
    assert f"export XDG_STATE_HOME={root / 'state'}" in out
    assert "export OVID2_WEB_BIND=127.0.0.1:18100" in out
    assert "unset OVID2_SCHEDULER_DB" in out


@pytest.mark.unit
def test_parser_wires_dev_subcommands() -> None:
    from ovid2.cli.__main__ import _build_parser

    parser = _build_parser()
    for argv in (
        ["dev", "up"],
        ["dev", "down", "x", "--purge"],
        ["dev", "ls", "--all"],
        ["dev", "logs", "x", "worker", "-f"],
        ["dev", "env"],
        ["dev", "restart"],
    ):
        args = parser.parse_args(argv)
        assert args._command == "dev"
