"""Unit tests for the disc-connection widening in connections_svc.

These focus on the read-side compat + write-side strict validation
that came in with the disc-mount / path-picker phase. The pre-existing
:mod:`tests.unit.web.test_connections_svc` still owns the general
CRUD suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.models import ConnectionWrite
from ovid2.web.services import connections_svc


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the XDG dirs at a fresh temp tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR", str(tmp_path / "config" / "ovid2" / "connections")
    )
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    return conns


class TestLegacyReadCompat:
    def test_legacy_root_path_surfaces_as_host_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = _setup(tmp_path, monkeypatch)
        (conns / "legacy_disc.json").write_text(
            json.dumps({"type": "disc", "args": {"root_path": "/tmp/legacy"}}),
            encoding="utf-8",
        )
        detail = connections_svc.get_connection("legacy_disc")
        assert detail is not None
        assert detail.args["host_path"] == "/tmp/legacy"
        assert detail.args["container_path"] == "/tmp/legacy"
        assert detail.args["mount_mode"] == "rw"

    def test_new_shape_read_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = _setup(tmp_path, monkeypatch)
        (conns / "new_disc.json").write_text(
            json.dumps({
                "type": "disc",
                "args": {
                    "host_path": "/host/x",
                    "container_path": "/mnt/x",
                    "mount_mode": "ro",
                },
            }),
            encoding="utf-8",
        )
        detail = connections_svc.get_connection("new_disc")
        assert detail is not None
        assert detail.args == {
            "host_path": "/host/x",
            "container_path": "/mnt/x",
            "mount_mode": "ro",
        }

    def test_legacy_file_untouched_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = _setup(tmp_path, monkeypatch)
        path = conns / "legacy_disc.json"
        path.write_text(
            json.dumps({"type": "disc", "args": {"root_path": "/keep_me"}}),
            encoding="utf-8",
        )
        original_bytes = path.read_bytes()
        connections_svc.get_connection("legacy_disc")
        # Read-only compat: the on-disk file is not rewritten.
        assert path.read_bytes() == original_bytes


class TestNewShapeWriteValidation:
    def test_write_accepts_new_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = _setup(tmp_path, monkeypatch)
        host = tmp_path / "host"
        host.mkdir()
        payload = ConnectionWrite(
            type="disc",
            args={
                "host_path": str(host),
                "container_path": str(host),
                "mount_mode": "rw",
            },
            secrets={},
        )
        connections_svc.write_connection("d1", payload, actor="test", create_only=True)
        written = json.loads((conns / "d1.json").read_text(encoding="utf-8"))
        assert written["args"]["host_path"] == str(host)
        assert written["args"]["container_path"] == str(host)
        assert written["args"]["mount_mode"] == "rw"

    def test_write_rejects_unknown_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup(tmp_path, monkeypatch)
        payload = ConnectionWrite(
            type="disc",
            args={"host_path": "/x", "not_a_real_key": "abc"},
            secrets={},
        )
        with pytest.raises(connections_svc.ConnectionError):
            connections_svc.write_connection(
                "bad", payload, actor="test", create_only=True
            )

    def test_write_rejects_missing_host_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup(tmp_path, monkeypatch)
        payload = ConnectionWrite(type="disc", args={}, secrets={})
        with pytest.raises(connections_svc.ConnectionError):
            connections_svc.write_connection(
                "empty", payload, actor="test", create_only=True
            )
