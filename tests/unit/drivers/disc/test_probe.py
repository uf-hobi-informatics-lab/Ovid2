"""Unit tests for :mod:`ovid2.drivers.disc.probe`."""

from __future__ import annotations

import os
from pathlib import Path

from ovid2.drivers.disc.probe import (
    probe_auth_from_host_path,
    probe_read_from_host_path,
    probe_write_from_host_path,
)


class TestProbeAuth:
    def test_ok_on_readable_directory(self, tmp_path: Path) -> None:
        r = probe_auth_from_host_path(str(tmp_path), {"host_path": str(tmp_path)})
        assert r.ok is True
        assert r.code == "ok"
        assert r.capability == "auth"

    def test_config_when_empty(self) -> None:
        r = probe_auth_from_host_path("", {})
        assert r.ok is False
        assert r.code == "config"

    def test_unreachable_on_missing_dir(self) -> None:
        r = probe_auth_from_host_path("/nonexistent-xyz-123", {})
        assert r.ok is False
        assert r.code == "unreachable"

    def test_auth_on_unreadable_dir(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        try:
            r = probe_auth_from_host_path(str(blocked), {})
            assert r.ok is False
            assert r.code == "auth"
        finally:
            os.chmod(blocked, 0o755)  # noqa: S103 — restoring pre-test mode

    def test_detail_carries_normalised_shape(self, tmp_path: Path) -> None:
        detail = {"host_path": str(tmp_path), "mount_mode": "ro"}
        r = probe_auth_from_host_path(str(tmp_path), detail)
        assert r.detail == detail


class TestProbeRead:
    def test_ok_on_listable_dir(self, tmp_path: Path) -> None:
        r = probe_read_from_host_path(str(tmp_path), {"host_path": str(tmp_path)})
        assert r.ok is True
        assert r.capability == "read"

    def test_falls_through_when_auth_fails(self) -> None:
        r = probe_read_from_host_path("/nonexistent-xyz-123", {})
        assert r.ok is False
        assert r.code == "unreachable"
        assert r.capability == "read"


class TestProbeWrite:
    def test_ok_on_writable_dir(self, tmp_path: Path) -> None:
        r = probe_write_from_host_path(str(tmp_path), {
            "host_path": str(tmp_path), "mount_mode": "rw",
        })
        assert r.ok is True
        assert r.capability == "write"
        # And the probe file is cleaned up.
        assert not any(f.name.startswith(".ovid2_probe_") for f in tmp_path.iterdir())

    def test_ro_mode_skips_probe(self, tmp_path: Path) -> None:
        r = probe_write_from_host_path(str(tmp_path), {
            "host_path": str(tmp_path), "mount_mode": "ro",
        })
        # ``skipped`` still returns ok=True so callers with a truthy
        # check don't complain, but the code makes the semantics clear.
        assert r.ok is True
        assert r.code == "skipped"
        assert r.capability == "write"

    def test_falls_through_when_auth_fails(self) -> None:
        r = probe_write_from_host_path("/nonexistent-xyz-123", {"mount_mode": "rw"})
        assert r.ok is False
        assert r.code == "unreachable"
        assert r.capability == "write"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
