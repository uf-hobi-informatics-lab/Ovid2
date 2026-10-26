"""Unit tests for the W4 address-config CRUD paths in ``addresses_svc``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.models import AddressWrite
from ovid2.web.services import addresses_svc


@pytest.mark.unit
def test_create_writes_disc_config(isolated_xdg: Path) -> None:
    """A create call writes a JSON file to the user-scoped configs dir.

    After the W7 connection-first flip, brand-new addresses must use
    the ``{connection, location}`` shape. Legacy-shape writes still
    work as updates (see ``test_update_preserves_masked_secret``
    below) so the 24 pre-W7 pipeline configs remain editable.
    """
    from ovid2.web.models import ConnectionWrite
    from ovid2.web.services import connections_svc

    connections_svc.write_connection(
        "conn_disc",
        ConnectionWrite(type="disc", args={"host_path": "/tmp"}),
        create_only=True,
    )
    result = addresses_svc.write_address(
        "disc_new",
        AddressWrite(connection="conn_disc", location={"path": "x"}),
        create_only=True,
    )
    assert result.action == "created"
    path = Path(result.path)
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload == {"connection": "conn_disc", "location": {"path": "x"}}


@pytest.mark.unit
def test_create_only_refuses_legacy_shape(isolated_xdg: Path) -> None:
    """W7: creating a new address with the legacy shape is refused."""
    with pytest.raises(addresses_svc.AddressError, match="no longer creatable"):
        addresses_svc.write_address(
            "legacy_new",
            AddressWrite(src_name="disc", src_args={"path": "/tmp/x"}),
            create_only=True,
        )


@pytest.mark.unit
def test_create_conflict(isolated_xdg: Path) -> None:
    """Creating twice with the same name raises AddressError."""
    from ovid2.web.models import ConnectionWrite
    from ovid2.web.services import connections_svc

    connections_svc.write_connection(
        "conn_disc",
        ConnectionWrite(type="disc", args={"host_path": "/tmp"}),
        create_only=True,
    )
    addresses_svc.write_address(
        "dup",
        AddressWrite(connection="conn_disc", location={"path": "x"}),
        create_only=True,
    )
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.write_address(
            "dup",
            AddressWrite(connection="conn_disc", location={"path": "y"}),
            create_only=True,
        )


@pytest.mark.unit
def test_update_preserves_masked_secret(isolated_xdg: Path) -> None:
    """A ``***`` value from the client keeps the on-disk password."""
    addresses_svc.write_address(
        "sf1",
        AddressWrite(
            src_name="snowflake",
            src_args={"account": "a", "user": "u", "password": "real"},
        ),
    )
    # Simulate the UI round-trip: read (which masks) then write.
    detail = addresses_svc.get_address("sf1")
    assert detail is not None
    assert detail.src_args["password"] == "***"
    addresses_svc.write_address(
        "sf1",
        AddressWrite(src_name="snowflake", src_args=detail.src_args),
    )
    path = Path(detail.path)
    raw = json.loads(path.read_text())
    assert raw["src_args"]["password"] == "real"


@pytest.mark.unit
def test_update_replaces_password(isolated_xdg: Path) -> None:
    """A non-sentinel value overwrites the on-disk password."""
    addresses_svc.write_address(
        "sf2",
        AddressWrite(
            src_name="snowflake",
            src_args={"account": "a", "user": "u", "password": "old"},
        ),
    )
    addresses_svc.write_address(
        "sf2",
        AddressWrite(
            src_name="snowflake",
            src_args={"account": "a", "user": "u", "password": "new"},
        ),
    )
    detail = addresses_svc.get_address("sf2")
    assert detail is not None
    path = Path(detail.path)
    raw = json.loads(path.read_text())
    assert raw["src_args"]["password"] == "new"


@pytest.mark.unit
def test_rejects_invalid_src_name(isolated_xdg: Path) -> None:
    """An unknown ``src_name`` is rejected."""
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.write_address("bad", AddressWrite(src_name="ftp", src_args={"path": "/x"}))


@pytest.mark.unit
def test_rejects_traversal_name(isolated_xdg: Path) -> None:
    """Path-traversal-ish names are rejected."""
    for bad in ("../evil", "with/slash", " space", "", ".", ".hidden"):
        with pytest.raises(addresses_svc.AddressError):
            addresses_svc.write_address(bad, AddressWrite(src_name="disc", src_args={"path": "/x"}))


@pytest.mark.unit
def test_disc_requires_path(isolated_xdg: Path) -> None:
    """disc addresses without a ``path`` are refused."""
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.write_address(
            "empty",
            AddressWrite(src_name="disc", src_args={}),
        )


@pytest.mark.unit
def test_snowflake_requires_user(isolated_xdg: Path) -> None:
    """snowflake addresses without a ``user`` are refused."""
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.write_address(
            "sf_no_user",
            AddressWrite(src_name="snowflake", src_args={"account": "a"}),
        )


@pytest.mark.unit
def test_masked_but_no_disk_value_refused(isolated_xdg: Path) -> None:
    """Sending ``***`` on create (no disk value to preserve) is rejected."""
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.write_address(
            "sf_masked",
            AddressWrite(
                src_name="snowflake",
                src_args={"account": "a", "user": "u", "password": "***"},
            ),
        )


@pytest.mark.unit
def test_delete_removes_file(isolated_xdg: Path) -> None:
    """Delete unlinks the on-disk config."""
    addresses_svc.write_address(
        "gone",
        AddressWrite(src_name="disc", src_args={"path": "/x"}),
    )
    result = addresses_svc.delete_address("gone")
    assert result.action == "deleted"
    assert addresses_svc.get_address("gone") is None


@pytest.mark.unit
def test_delete_missing_raises(isolated_xdg: Path) -> None:
    """Deleting an unknown address raises AddressError."""
    with pytest.raises(addresses_svc.AddressError):
        addresses_svc.delete_address("nonexistent")


@pytest.mark.unit
def test_audit_row_written(isolated_xdg: Path) -> None:
    """Every mutation appends a ``web_audit`` row."""
    from ovid2.web import db as web_db

    addresses_svc.write_address(
        "audited",
        AddressWrite(src_name="disc", src_args={"path": "/x"}),
    )
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT action, target FROM web_audit WHERE target = ?", ("audited",)
        ).fetchall()
    actions = {row["action"] for row in rows}
    assert "address.created" in actions
