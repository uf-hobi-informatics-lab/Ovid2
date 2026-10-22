"""Unit tests for ``addresses_svc.resolve_or_create_address`` (browse-mode submit)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.services import addresses_svc as svc


def _write_disc_connection(xdg: Path, name: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": "/srv/data",
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        )
    )


def _write_snowflake_connection(xdg: Path, name: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "snowflake",
                "args": {"account": name, "user": "OVIDCONNECTOR"},
            }
        )
    )


@pytest.mark.unit
def test_slugify_prefers_path() -> None:
    assert svc._slugify_location({"path": "demo/20260101"}) == "demo_20260101"


@pytest.mark.unit
def test_slugify_empty_is_root() -> None:
    assert svc._slugify_location({}) == "root"


@pytest.mark.unit
def test_normalise_drops_empty() -> None:
    assert svc._normalise_location({"path": "x", "fmt": None, "e": ""}) == {
        "path": "x"
    }


@pytest.mark.unit
def test_resolve_creates_new_address(isolated_xdg: Path) -> None:
    _write_disc_connection(isolated_xdg, "local_disc")
    name = svc.resolve_or_create_address("local_disc", {"path": "demo/2026"})
    assert name == "local_disc__demo_2026"
    # The address file was written with the W7 shape.
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    doc = json.loads((configs / f"{name}.json").read_text())
    assert doc["connection"] == "local_disc"
    assert doc["location"] == {"path": "demo/2026"}


@pytest.mark.unit
def test_resolve_reuses_identical_address(isolated_xdg: Path) -> None:
    _write_disc_connection(isolated_xdg, "local_disc")
    first = svc.resolve_or_create_address("local_disc", {"path": "demo/2026"})
    second = svc.resolve_or_create_address("local_disc", {"path": "demo/2026"})
    assert first == second
    # No duplicate file was created.
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    matches = list(configs.glob("local_disc__*.json"))
    assert len(matches) == 1


@pytest.mark.unit
def test_resolve_empty_location(isolated_xdg: Path) -> None:
    _write_disc_connection(isolated_xdg, "local_disc")
    name = svc.resolve_or_create_address("local_disc", None)
    assert name == "local_disc__root"


@pytest.mark.unit
def test_resolve_requires_connection() -> None:
    with pytest.raises(svc.AddressError):
        svc.resolve_or_create_address("", {"path": "x"})


@pytest.mark.unit
def test_find_matching_address_none_when_absent(isolated_xdg: Path) -> None:
    _write_disc_connection(isolated_xdg, "local_disc")
    assert svc.find_matching_address("local_disc", {"path": "nope"}) is None


@pytest.mark.unit
def test_find_matching_ignores_legacy(isolated_xdg: Path) -> None:
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "legacy.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "x"}})
    )
    assert svc.find_matching_address("disc", {"path": "x"}) is None


@pytest.mark.unit
def test_connection_type_reads_registered_type(isolated_xdg: Path) -> None:
    _write_snowflake_connection(isolated_xdg, "wh")
    assert svc.connection_type("wh") == "snowflake"
    assert svc.is_warehouse_connection("wh") is True


@pytest.mark.unit
def test_connection_type_unknown_is_not_warehouse(isolated_xdg: Path) -> None:
    # Missing connection classifies as non-warehouse (fail-open) so a genuine
    # filesystem run is never blocked by a lookup miss.
    assert svc.connection_type("nope") is None
    assert svc.is_warehouse_connection("nope") is False


@pytest.mark.unit
def test_resolve_warehouse_empty_location_rejected(isolated_xdg: Path) -> None:
    # Reproduces the DC_CHECKS __root failure: a Snowflake connection with no
    # database/schema must NOT resolve to an empty-location address.
    _write_snowflake_connection(isolated_xdg, "acme-azure_eastus2")
    with pytest.raises(svc.AddressError, match="no runnable root"):
        svc.resolve_or_create_address("acme-azure_eastus2", {})
    with pytest.raises(svc.AddressError, match="no runnable root"):
        svc.resolve_or_create_address("acme-azure_eastus2", None)
    # No __root address was written.
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    assert not list(configs.glob("acme-azure_eastus2__root.json"))


@pytest.mark.unit
def test_resolve_warehouse_with_database_ok(isolated_xdg: Path) -> None:
    _write_snowflake_connection(isolated_xdg, "wh")
    name = svc.resolve_or_create_address("wh", {"path": "CHAR_DEMO"})
    assert name == "wh__char_demo"
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    doc = json.loads((configs / f"{name}.json").read_text())
    assert doc["location"] == {"path": "CHAR_DEMO"}


@pytest.mark.unit
def test_resolve_filesystem_empty_root_still_allowed(isolated_xdg: Path) -> None:
    # Regression guard: disc/s3 keep their valid root; only warehouses are
    # blocked at empty location.
    _write_disc_connection(isolated_xdg, "local_disc")
    assert svc.resolve_or_create_address("local_disc", {}) == "local_disc__root"
