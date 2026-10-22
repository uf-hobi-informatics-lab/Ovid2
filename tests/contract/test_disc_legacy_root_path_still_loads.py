"""Contract: legacy ``root_path`` disc connection JSONs still load.

The disc-mount / path-picker phase widened the disc schema from
``{root_path}`` to ``{host_path, container_path, mount_mode}`` but
committed to read-only compat: no on-disk migration, ``root_path``
JSONs keep working forever.

This test verifies two things:

1. :func:`ovid2.connections.disc.normalise_disc_args` widens a
   legacy dict into the full triple.
2. :func:`ovid2.web.services.connections_svc.get_connection` returns
   the widened view when reading a legacy JSON.
3. The on-disk file is *not* rewritten by the read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.contract
def test_legacy_root_path_widens_at_read_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ovid2.drivers.disc import normalise_disc_args

    widened = normalise_disc_args({"root_path": "/legacy_root"})
    assert widened == {
        "host_path": "/legacy_root",
        "container_path": "/legacy_root",
        "mount_mode": "rw",
    }


@pytest.mark.contract
def test_legacy_disc_connection_loads_via_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    file = conns / "legacy.json"
    original = json.dumps({"type": "disc", "args": {"root_path": "/x"}})
    file.write_text(original, encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conns))

    from ovid2.web.services import connections_svc

    detail = connections_svc.get_connection("legacy")
    assert detail is not None
    assert detail.args["host_path"] == "/x"
    assert detail.args["container_path"] == "/x"
    assert detail.args["mount_mode"] == "rw"

    # And the on-disk file is untouched.
    assert file.read_text(encoding="utf-8") == original


@pytest.mark.contract
def test_legacy_disc_connection_still_instantiates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline-side loader must still be able to instantiate a legacy disc."""
    host = tmp_path / "legacy_root"
    host.mkdir()
    conns = tmp_path / "config" / "ovid2" / "connections"
    conns.mkdir(parents=True, exist_ok=True)
    (conns / "legacy.json").write_text(
        json.dumps({"type": "disc", "args": {"root_path": str(host)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conns))

    from ovid2.config import connections as _connections

    connection = _connections.load_connection("legacy")
    # New-shape properties available.
    assert connection.host_path == str(host)
    assert connection.container_path == str(host)
    assert connection.mount_mode == "rw"
    # And test() reports OK.
    report = connection.test()
    assert report.ok
