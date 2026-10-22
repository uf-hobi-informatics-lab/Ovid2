"""Unit tests for the disc connection widening + auto-mount helper."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.drivers.disc import DiscConnection, normalise_disc_args


class TestNormaliseDiscArgs:
    def test_new_shape_round_trips(self) -> None:
        got = normalise_disc_args({
            "host_path": "/a",
            "container_path": "/b",
            "mount_mode": "ro",
        })
        assert got == {"host_path": "/a", "container_path": "/b", "mount_mode": "ro"}

    def test_container_defaults_to_host(self) -> None:
        got = normalise_disc_args({"host_path": "/a"})
        assert got["host_path"] == "/a"
        assert got["container_path"] == "/a"
        assert got["mount_mode"] == "rw"

    def test_legacy_root_path_widens_into_host(self) -> None:
        got = normalise_disc_args({"root_path": "/legacy"})
        assert got == {
            "host_path": "/legacy",
            "container_path": "/legacy",
            "mount_mode": "rw",
        }

    def test_host_path_wins_over_root_path(self) -> None:
        got = normalise_disc_args({"host_path": "/new", "root_path": "/old"})
        assert got["host_path"] == "/new"
        assert got["container_path"] == "/new"

    def test_bad_mount_mode_falls_back_to_rw(self) -> None:
        got = normalise_disc_args({"host_path": "/x", "mount_mode": "chaos"})
        assert got["mount_mode"] == "rw"

    def test_ro_is_accepted(self) -> None:
        got = normalise_disc_args({"host_path": "/x", "mount_mode": "ro"})
        assert got["mount_mode"] == "ro"

    def test_all_missing_returns_empty_host(self) -> None:
        got = normalise_disc_args({})
        assert got == {"host_path": "", "container_path": "", "mount_mode": "rw"}


class TestDiscConnectionTest:
    def test_test_reports_ok_on_existing_host_dir(self, tmp_path: Path) -> None:
        conn = DiscConnection(args={"host_path": str(tmp_path)}, secrets={})
        report = conn.test()
        assert report.ok
        assert report.code == "ok"

    def test_test_reports_unreachable_on_missing_dir(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        conn = DiscConnection(args={"host_path": str(missing)}, secrets={})
        report = conn.test()
        assert not report.ok
        assert report.code == "unreachable"

    def test_test_config_when_empty(self) -> None:
        conn = DiscConnection(args={}, secrets={})
        report = conn.test()
        assert not report.ok
        assert report.code == "config"

    def test_test_accepts_legacy_root_path(self, tmp_path: Path) -> None:
        conn = DiscConnection(args={"root_path": str(tmp_path)}, secrets={})
        report = conn.test()
        assert report.ok


class TestDiscAsAddressConfig:
    def test_uses_container_path_when_different(self, tmp_path: Path) -> None:
        conn = DiscConnection(
            args={"host_path": str(tmp_path), "container_path": "/mnt/host_data"},
            secrets={},
        )
        result = conn.as_address_config({"path": "demo/20260316"})
        # The pipeline sees the container_path, not the host_path.
        assert result["path"] == "/mnt/host_data/demo/20260316"

    def test_falls_back_to_host_when_container_absent(self, tmp_path: Path) -> None:
        conn = DiscConnection(args={"host_path": str(tmp_path)}, secrets={})
        result = conn.as_address_config({"path": "demo/x"})
        assert result["path"] == f"{tmp_path}/demo/x"

    def test_empty_location_returns_root_only(self, tmp_path: Path) -> None:
        conn = DiscConnection(args={"host_path": str(tmp_path)}, secrets={})
        result = conn.as_address_config({})
        assert result == {"path": str(tmp_path)}

    def test_format_and_lowercase_forwarded(self, tmp_path: Path) -> None:
        conn = DiscConnection(args={"host_path": str(tmp_path)}, secrets={})
        result = conn.as_address_config(
            {"path": "s", "format": "csv", "lowercase_columns": True}
        )
        assert result["format"] == "csv"
        assert result["lowercase_columns"] is True


class TestDiscMountBuilder:
    """``build_disc_mounts`` and ``disc_mount_for_connection`` behaviour."""

    def _write_disc(self, tmp_path: Path, name: str, args: dict[str, object]) -> None:
        conns = tmp_path / "config" / "ovid2" / "connections"
        conns.mkdir(parents=True, exist_ok=True)
        (conns / f"{name}.json").write_text(
            json.dumps({"type": "disc", "args": args}), encoding="utf-8"
        )

    def _write_addr(
        self, tmp_path: Path, name: str, payload: dict[str, object]
    ) -> None:
        cfgs = tmp_path / "config" / "ovid2" / "configs"
        cfgs.mkdir(parents=True, exist_ok=True)
        (cfgs / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_auto_mount_created_for_new_shape_disc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = tmp_path / "host_root"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(
            tmp_path,
            "sandbox_disc",
            {"host_path": str(host), "container_path": str(host), "mount_mode": "rw"},
        )
        self._write_addr(
            tmp_path,
            "sandbox_out",
            {"connection": "sandbox_disc", "location": {"path": "subdir"}},
        )
        from ovid2.connections.disc_mounts import build_disc_mounts

        mounts = build_disc_mounts(["sandbox_out"])
        assert len(mounts) == 1
        assert mounts[0].host == str(host)
        assert mounts[0].container == str(host)
        assert mounts[0].mode == "rw"

    def test_explicit_override_suppresses_auto_mount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        host = tmp_path / "host_root"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(tmp_path, "d", {"host_path": str(host)})
        self._write_addr(
            tmp_path, "a", {"connection": "d", "location": {}}
        )
        from ovid2.connections.disc_mounts import build_disc_mounts

        caplog.set_level("WARNING")
        mounts = build_disc_mounts(
            ["a"], explicit_container_paths=[str(host)]
        )
        assert mounts == []
        assert any("overrides auto-mount" in r.message for r in caplog.records)

    def test_container_path_can_differ_from_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = tmp_path / "on_host"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(
            tmp_path,
            "d",
            {"host_path": str(host), "container_path": "/mnt/sink"},
        )
        self._write_addr(tmp_path, "a", {"connection": "d", "location": {}})
        from ovid2.connections.disc_mounts import build_disc_mounts

        mounts = build_disc_mounts(["a"])
        assert len(mounts) == 1
        assert mounts[0].host == str(host)
        assert mounts[0].container == "/mnt/sink"

    def test_reserved_container_path_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = tmp_path / "d1"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(
            tmp_path, "d", {"host_path": str(host), "container_path": "/data"}
        )
        self._write_addr(tmp_path, "a", {"connection": "d", "location": {}})
        from ovid2.connections.disc_mounts import build_disc_mounts

        assert build_disc_mounts(["a"]) == []

    def test_nonexistent_host_path_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(tmp_path, "d", {"host_path": "/does/not/exist/here"})
        self._write_addr(tmp_path, "a", {"connection": "d", "location": {}})
        from ovid2.connections.disc_mounts import build_disc_mounts

        assert build_disc_mounts(["a"]) == []

    def test_non_disc_address_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        # legacy shape, non-disc
        self._write_addr(
            tmp_path,
            "sf",
            {"src_name": "snowflake", "src_args": {"database": "X"}},
        )
        from ovid2.connections.disc_mounts import build_disc_mounts

        assert build_disc_mounts(["sf"]) == []

    def test_legacy_root_path_disc_still_auto_mounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = tmp_path / "legacy_root"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(tmp_path, "d", {"root_path": str(host)})
        self._write_addr(tmp_path, "a", {"connection": "d", "location": {}})
        from ovid2.connections.disc_mounts import build_disc_mounts

        mounts = build_disc_mounts(["a"])
        assert len(mounts) == 1
        assert mounts[0].host == str(host)
        assert mounts[0].container == str(host)

    def test_deduplicates_across_from_and_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = tmp_path / "shared"
        host.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self._write_disc(tmp_path, "d", {"host_path": str(host)})
        # Two different addresses sharing the same disc connection.
        self._write_addr(
            tmp_path, "a1", {"connection": "d", "location": {"path": "one"}}
        )
        self._write_addr(
            tmp_path, "a2", {"connection": "d", "location": {"path": "two"}}
        )
        from ovid2.connections.disc_mounts import build_disc_mounts

        mounts = build_disc_mounts(["a1", "a2"])
        assert len(mounts) == 1

    def test_missing_address_is_silently_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        from ovid2.connections.disc_mounts import build_disc_mounts

        assert build_disc_mounts(["does_not_exist"]) == []
