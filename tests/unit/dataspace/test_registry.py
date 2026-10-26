"""Tests for :mod:`ovid2.dataspace.registry`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.connections.registry import CONNECTION_TYPE_MAP
from ovid2.dataspace import registry as reg


@pytest.fixture()
def configs_dir(isolated_xdg: Path) -> Path:
    """Where address configs go on the search path."""
    return Path(os.environ["OVID2_CONFIGS_DIR"])


@pytest.fixture()
def connections_dir(isolated_xdg: Path) -> Path:
    """Where connection configs live."""
    return Path(os.environ["OVID2_CONNECTIONS_DIR"])


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestListDimensions:
    def test_matches_connection_types(self) -> None:
        assert sorted(reg.list_dimensions()) == sorted(CONNECTION_TYPE_MAP.keys())


class TestDimensionForAddress:
    def test_legacy_disc(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "disc_input.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/x"}},
        )
        assert reg.dimension_for_address("disc_input") == "disc"

    def test_legacy_snowflake(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "sf_out.json",
            {"src_name": "snowflake", "src_args": {"account": "abc"}},
        )
        assert reg.dimension_for_address("sf_out") == "snowflake"

    def test_connection_shape(self, configs_dir: Path, connections_dir: Path) -> None:
        _write_json(
            connections_dir / "disc_prod.json",
            {"type": "disc", "args": {"host_path": "/data"}},
        )
        _write_json(
            configs_dir / "disc_view.json",
            {"connection": "disc_prod", "location": {"path": ""}},
        )
        assert reg.dimension_for_address("disc_view") == "disc"

    def test_missing_config(self) -> None:
        assert reg.dimension_for_address("nonexistent") is None

    def test_bogus_src_name(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "weird.json",
            {"src_name": "not_a_type", "src_args": {}},
        )
        assert reg.dimension_for_address("weird") is None


class TestListAddressesByDimension:
    def test_empty(self, configs_dir: Path) -> None:
        assert reg.list_addresses_by_dimension("disc") == []

    def test_populated(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d_a.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/a"}},
        )
        _write_json(
            configs_dir / "d_b.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/b"}},
        )
        _write_json(
            configs_dir / "sf_x.json",
            {"src_name": "snowflake", "src_args": {"account": "acc"}},
        )
        assert reg.list_addresses_by_dimension("disc") == ["d_a", "d_b"]
        assert reg.list_addresses_by_dimension("snowflake") == ["sf_x"]

    def test_unknown_dimension_raises(self) -> None:
        with pytest.raises(KeyError):
            reg.list_addresses_by_dimension("nonsense")
