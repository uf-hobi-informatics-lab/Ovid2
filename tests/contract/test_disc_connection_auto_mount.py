"""Contract: disc connections auto-generate a bind mount when referenced.

The disc-mount / path-picker phase established that any address
whose underlying connection is ``disc`` must contribute a matching
``ExtraMount`` to the per-invocation container's ``volumes:`` list.
Without this, an ETL job whose feed lives outside the standard mount
set (``/data``, ``/app``, ``/ovid_credentials``, ``/usr/include/rsa``)
fails at container-side ``mkdir`` walk-up with a ``PermissionError``.

This test synthesises a disc connection + address pair on a temp
tree and asserts the compose builder ends up with the expected
bind-mount line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.contract
def test_disc_connection_auto_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disc address must contribute an ExtraMount via build_disc_mounts."""
    host = tmp_path / "host_root"
    host.mkdir()
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    cfgs = tmp_path / "config" / "ovid2" / "configs"
    cfgs.mkdir(parents=True, exist_ok=True)

    (conns / "sandbox_disc.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {"host_path": str(host), "mount_mode": "rw"},
            }
        ),
        encoding="utf-8",
    )
    (cfgs / "sandbox_out.json").write_text(
        json.dumps(
            {"connection": "sandbox_disc", "location": {"path": "batch"}}
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    from ovid2.connections.disc_mounts import build_disc_mounts

    mounts = build_disc_mounts(["sandbox_out"])
    assert len(mounts) == 1
    m = mounts[0]
    assert m.host == str(host)
    assert m.container == str(host)  # mirror-by-default
    assert m.mode == "rw"

    # And the compose volume string carries all three parts.
    volume_entry = m.as_compose_volume()
    assert volume_entry == f"{host}:{host}:rw"


@pytest.mark.contract
def test_disc_connection_container_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When container_path differs from host_path, the mount reflects it."""
    host = tmp_path / "on_host"
    host.mkdir()
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    cfgs = tmp_path / "config" / "ovid2" / "configs"
    cfgs.mkdir(parents=True, exist_ok=True)

    (conns / "d.json").write_text(
        json.dumps({
            "type": "disc",
            "args": {
                "host_path": str(host),
                "container_path": "/mnt/sink",
                "mount_mode": "ro",
            },
        }),
        encoding="utf-8",
    )
    (cfgs / "a.json").write_text(
        json.dumps({"connection": "d", "location": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    from ovid2.connections.disc_mounts import build_disc_mounts

    mounts = build_disc_mounts(["a"])
    assert len(mounts) == 1
    assert mounts[0].host == str(host)
    assert mounts[0].container == "/mnt/sink"
    assert mounts[0].mode == "ro"


@pytest.mark.contract
def test_disc_connection_reserved_container_path_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disc connection targeting /data must NOT be auto-mounted."""
    host = tmp_path / "h"
    host.mkdir()
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    cfgs = tmp_path / "config" / "ovid2" / "configs"
    cfgs.mkdir(parents=True, exist_ok=True)
    (conns / "d.json").write_text(
        json.dumps({
            "type": "disc",
            "args": {"host_path": str(host), "container_path": "/data"},
        }),
        encoding="utf-8",
    )
    (cfgs / "a.json").write_text(
        json.dumps({"connection": "d", "location": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    from ovid2.connections.disc_mounts import build_disc_mounts

    assert build_disc_mounts(["a"]) == []
