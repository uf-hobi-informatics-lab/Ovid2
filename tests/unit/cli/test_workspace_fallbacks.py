"""Unit tests for :mod:`ovid2.cli._workspace` (engine scratch-database fallbacks)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ovid2.cli._workspace import (
    workspace_databases_from_addresses,
    workspace_databases_from_drivers,
)


class _Sf:
    @classmethod
    def workspace_database(cls, location: dict[str, Any] | None) -> str | None:
        from ovid2.drivers.snowflake.driver import SnowflakeDriver

        return SnowflakeDriver.workspace_database(location)

    def __init__(self, location: dict[str, Any]) -> None:
        self.location = location


class _Disc:
    @classmethod
    def workspace_database(cls, location: dict[str, Any] | None) -> str | None:
        return None

    location: dict[str, Any] = {"path": "x"}


@pytest.mark.unit
def test_snowflake_workspace_database_reads_key_or_path() -> None:
    from ovid2.drivers.snowflake.driver import SnowflakeDriver

    assert SnowflakeDriver.workspace_database({"database": "CHAR_X", "schema": "S"}) == "CHAR_X"
    assert SnowflakeDriver.workspace_database({"path": "CHAR_ACME/PUBLIC"}) == "CHAR_ACME"
    assert SnowflakeDriver.workspace_database({}) is None
    assert SnowflakeDriver.workspace_database(None) is None


@pytest.mark.unit
def test_non_warehouse_driver_has_no_workspace_database() -> None:
    from ovid2.drivers.disc.driver import DiscDriver

    assert DiscDriver.workspace_database({"path": "/data/x"}) is None


@pytest.mark.unit
def test_drivers_ordered_deduplicated_and_disc_skipped() -> None:
    out = workspace_databases_from_drivers(
        [
            _Disc(),
            _Sf({"path": "CHAR_ACME/PUBLIC"}),
            None,
            _Sf({"database": "char_acme"}),
            _Sf({"database": "SANDBOX"}),
        ]
    )
    assert out == ["CHAR_ACME", "SANDBOX"]


@pytest.mark.unit
def test_from_addresses_uses_real_loader_and_skips_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = tmp_path / "configs"
    conns = tmp_path / "connections"
    configs.mkdir()
    conns.mkdir()
    (conns / "sf.json").write_text(
        json.dumps({"type": "snowflake", "args": {"account": "demo", "user": "u"}})
    )
    (conns / "dsk.json").write_text(json.dumps({"type": "disc", "args": {"path": str(tmp_path)}}))
    (configs / "sf_in.json").write_text(
        json.dumps({"connection": "sf", "location": {"path": "CHAR_DEMO/PUBLIC"}})
    )
    (configs / "disc_out.json").write_text(json.dumps({"connection": "dsk", "location": {}}))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conns))

    out = workspace_databases_from_addresses(
        ["disc_out", "sf_in", "does_not_exist", None], pipeline="no_such_pipeline"
    )
    assert out == ["CHAR_DEMO"]
