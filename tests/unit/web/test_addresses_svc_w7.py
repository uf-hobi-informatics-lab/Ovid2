"""Tests for the W7 widening of the address CRUD service (connection shape)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.models import AddressWrite, ConnectionWrite
from ovid2.web.services import addresses_svc, connections_svc


def _make_connection(isolated_xdg: Path) -> None:
    """Create a disc connection so address writes can reference it."""
    connections_svc.write_connection(
        "conn_disc",
        ConnectionWrite(type="disc", args={"host_path": str(isolated_xdg)}),
        actor="test",
        create_only=True,
    )


@pytest.mark.unit
def test_new_shape_write(isolated_xdg: Path) -> None:
    _make_connection(isolated_xdg)
    r = addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "sub/dir"}),
        actor="test",
        create_only=True,
    )
    assert r.action == "created"
    payload = json.loads(Path(r.path).read_text())
    assert payload["connection"] == "conn_disc"
    assert payload["location"] == {"path": "sub/dir"}
    assert "src_name" not in payload
    assert "src_args" not in payload


@pytest.mark.unit
def test_new_shape_reads_back_via_get(isolated_xdg: Path) -> None:
    _make_connection(isolated_xdg)
    addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "sub"}),
        actor="test",
        create_only=True,
    )
    detail = addresses_svc.get_address("addr_new")
    assert detail is not None
    assert detail.connection == "conn_disc"
    assert detail.location == {"path": "sub"}
    assert detail.src_name == "disc"


@pytest.mark.unit
def test_new_shape_summary(isolated_xdg: Path) -> None:
    _make_connection(isolated_xdg)
    addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "sub"}),
        actor="test",
        create_only=True,
    )
    summaries = {s.name: s for s in addresses_svc.list_addresses()}
    assert "addr_new" in summaries
    assert summaries["addr_new"].connection == "conn_disc"
    assert summaries["addr_new"].src_name == "disc"


@pytest.mark.unit
def test_new_shape_missing_connection_rejected(isolated_xdg: Path) -> None:
    with pytest.raises(addresses_svc.AddressError, match="not on the config search path"):
        addresses_svc.write_address(
            "addr_bad",
            AddressWrite(connection="nonexistent", location={"path": "x"}),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_mixed_shape_rejected(isolated_xdg: Path) -> None:
    with pytest.raises(addresses_svc.AddressError, match="mixes shapes"):
        addresses_svc.write_address(
            "addr_bad",
            AddressWrite(
                src_name="disc",
                src_args={"path": "/tmp"},
                connection="c",
                location={"path": "x"},
            ),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_empty_payload_rejected(isolated_xdg: Path) -> None:
    with pytest.raises(addresses_svc.AddressError, match="empty"):
        addresses_svc.write_address(
            "addr_bad",
            AddressWrite(),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_legacy_create_refused(isolated_xdg: Path) -> None:
    """W7 flip: creating a fresh legacy-shape address is refused."""
    with pytest.raises(addresses_svc.AddressError, match="no longer creatable"):
        addresses_svc.write_address(
            "addr_legacy",
            AddressWrite(src_name="disc", src_args={"path": "/tmp/legacy"}),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_legacy_edit_round_trip(isolated_xdg: Path) -> None:
    """Existing legacy files stay editable: an update (create_only=False) is honoured.

    Regression guard: the 24 pre-W7 pipeline configs must remain
    manageable through the web tier even after the connection-first
    flip. The refusal only fires on ``create_only=True``.
    """
    # Land a legacy config on disk directly — mimics a pre-existing file.
    import os

    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "addr_legacy.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/legacy"}}),
        encoding="utf-8",
    )
    r = addresses_svc.write_address(
        "addr_legacy",
        AddressWrite(src_name="disc", src_args={"path": "/tmp/legacy2"}),
        actor="test",
    )
    assert r.action == "updated"
    payload = json.loads(Path(r.path).read_text())
    assert payload["src_name"] == "disc"
    assert payload["src_args"] == {"path": "/tmp/legacy2"}


@pytest.mark.unit
def test_is_legacy_shape_detail(isolated_xdg: Path) -> None:
    """``is_legacy_shape`` classifies AddressDetail / dict / None inputs."""
    _make_connection(isolated_xdg)
    addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "x"}),
        actor="test",
        create_only=True,
    )
    new_detail = addresses_svc.get_address("addr_new")
    assert addresses_svc.is_legacy_shape(new_detail) is False

    import os

    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "legacy1.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/x"}}),
        encoding="utf-8",
    )
    old_detail = addresses_svc.get_address("legacy1")
    assert addresses_svc.is_legacy_shape(old_detail) is True

    assert addresses_svc.is_legacy_shape(None) is False
    assert addresses_svc.is_legacy_shape({"connection": "c"}) is False
    assert addresses_svc.is_legacy_shape({"src_name": "disc"}) is True


@pytest.mark.unit
def test_config_loader_resolves_new_shape(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_address_config on a new-shape config marks _shape='connection'."""
    _make_connection(isolated_xdg)
    addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "sub"}),
        actor="test",
        create_only=True,
    )
    from ovid2.config import addresses as _addresses

    cfg = _addresses.load_address_config("addr_new")
    assert cfg["_shape"] == "connection"
    assert cfg["connection"] == "conn_disc"
    assert cfg["location"] == {"path": "sub"}


@pytest.mark.unit
def test_load_address_dereferences_connection(isolated_xdg: Path) -> None:
    """The pipeline entry point returns a working DiscAddress via the shim."""
    _make_connection(isolated_xdg)
    addresses_svc.write_address(
        "addr_new",
        AddressWrite(connection="conn_disc", location={"path": "sub"}),
        actor="test",
        create_only=True,
    )
    from ovid2.config import addresses as _addresses

    addr = _addresses.load_address("addr_new")
    assert addr.__class__.__name__ == "DiscAddress"
    # Root path is combined with the location's sub-path.
    assert addr.root_path.endswith("/sub")
