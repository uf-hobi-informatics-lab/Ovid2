"""Tests for :mod:`ovid2.dataspace.resolver`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.dataspace import resolver
from ovid2.dataspace.path import OdsPath
from ovid2.dataspace.resolver import OdsResolutionError, resolve


@pytest.fixture()
def configs_dir(isolated_xdg: Path) -> Path:
    return Path(os.environ["OVID2_CONFIGS_DIR"])


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_settings(payload: dict) -> None:
    path = Path(os.environ["OVID2_SETTINGS_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    # Bust the settings_svc mtime cache.
    from ovid2.web.services import settings_svc

    settings_svc.invalidate_cache()


class TestResolveSoleCandidate:
    def test_single_disc_address_wins(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_settings({})
        target = resolve("/demo/20260316/DEMO", materialise=False)
        assert target.address_name == "d"
        assert target.dimension == "disc"
        assert target.tier == "sole_candidate"

    def test_multiple_candidates_without_default_raises(
        self, configs_dir: Path
    ) -> None:
        _write_json(
            configs_dir / "d1.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/1"}},
        )
        _write_json(
            configs_dir / "d2.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/2"}},
        )
        _write_settings({})
        with pytest.raises(OdsResolutionError):
            resolve("/demo/20260316/DEMO", materialise=False)

    def test_no_candidate_raises(self, configs_dir: Path) -> None:
        _write_settings({})
        with pytest.raises(OdsResolutionError):
            resolve("/demo", materialise=False)

    def test_root_path_raises(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_settings({})
        with pytest.raises(OdsResolutionError):
            resolve("/", materialise=False)


class TestResolvePrecedence:
    def test_dimension_default(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d1.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/1"}},
        )
        _write_json(
            configs_dir / "d2.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/2"}},
        )
        _write_settings({"ods": {"dimension_defaults": {"disc": "d2"}}})
        target = resolve("/demo", materialise=False)
        assert target.address_name == "d2"
        assert target.tier == "dimension_default"

    def test_pipeline_route_beats_dimension_default(
        self, configs_dir: Path
    ) -> None:
        _write_json(
            configs_dir / "d1.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/1"}},
        )
        _write_json(
            configs_dir / "d2.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/2"}},
        )
        _write_settings(
            {
                "ods": {
                    "dimension_defaults": {"disc": "d1"},
                    "pipeline_routes": {"demo": {"disc": "d2"}},
                }
            }
        )
        target = resolve("/demo", materialise=False)
        assert target.address_name == "d2"
        assert target.tier == "pipeline_route"

    def test_explicit_wins(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d1.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/1"}},
        )
        _write_json(
            configs_dir / "d2.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/2"}},
        )
        _write_settings(
            {"ods": {"dimension_defaults": {"disc": "d1"}}}
        )
        target = resolve("/demo", address="d2", materialise=False)
        assert target.address_name == "d2"
        assert target.tier == "explicit"

    def test_explicit_wrong_dimension_raises(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d1.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/1"}},
        )
        _write_json(
            configs_dir / "sf.json",
            {"src_name": "snowflake", "src_args": {"account": "a"}},
        )
        _write_settings({})
        with pytest.raises(OdsResolutionError):
            resolve("/demo", dim="disc", address="sf", materialise=False)

    def test_pipeline_dimension_default(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_json(
            configs_dir / "sf.json",
            {"src_name": "snowflake", "src_args": {"account": "a"}},
        )
        _write_settings(
            {"ods": {"pipeline_dimensions": {"demo": "snowflake"}}}
        )
        target = resolve("/demo", materialise=False)
        assert target.dimension == "snowflake"
        assert target.address_name == "sf"


class TestResolveDim:
    def test_unknown_dim(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_settings({})
        with pytest.raises(OdsResolutionError):
            resolve("/demo", dim="nonsense", materialise=False)

    def test_explicit_dim_used(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_settings({"ods": {"default_dimension": "snowflake"}})
        # pipeline default is snowflake, but explicit dim= overrides.
        target = resolve("/demo", dim="disc", materialise=False)
        assert target.dimension == "disc"


class TestListPipelinesAndBatches:
    def test_list_pipelines_walks_disc_root(
        self, tmp_path: Path, configs_dir: Path
    ) -> None:
        # Build a disc tree with two pipeline dirs.
        root = tmp_path / "disc"
        (root / "demo" / "20260316").mkdir(parents=True)
        (root / "ZETA" / "20260101").mkdir(parents=True)
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": str(root)}},
        )
        _write_settings({})
        names = resolver.list_pipelines(dim="disc")
        assert "demo" in names
        assert "ZETA" in names

    def test_list_batches(self, tmp_path: Path, configs_dir: Path) -> None:
        root = tmp_path / "disc"
        (root / "DEMO" / "20260101").mkdir(parents=True)
        (root / "DEMO" / "20260316").mkdir(parents=True)
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": str(root)}},
        )
        _write_settings({})
        assert resolver.list_batches("demo", dim="disc") == [
            "20260101",
            "20260316",
        ]

    def test_list_tables(self, tmp_path: Path, configs_dir: Path) -> None:
        root = tmp_path / "disc"
        batch = root / "DEMO" / "20260316"
        batch.mkdir(parents=True)
        (batch / "DEMOGRAPHIC.csv").write_text("a,b\n", encoding="utf-8")
        (batch / "ENCOUNTER.parquet").write_text("", encoding="utf-8")
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": str(root)}},
        )
        _write_settings({})
        assert resolver.list_tables("demo", "20260316", dim="disc") == [
            "DEMOGRAPHIC",
            "ENCOUNTER",
        ]

    def test_non_disc_dimensions_empty(
        self, tmp_path: Path, configs_dir: Path
    ) -> None:
        _write_json(
            configs_dir / "sf.json",
            {"src_name": "snowflake", "src_args": {"account": "a"}},
        )
        _write_settings({})
        assert resolver.list_batches("demo", dim="snowflake") == []
        assert resolver.list_tables("demo", "20260316", dim="snowflake") == []


class TestOdsPathIntegration:
    def test_path_object_accepted(self, configs_dir: Path) -> None:
        _write_json(
            configs_dir / "d.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/d"}},
        )
        _write_settings({})
        target = resolve(OdsPath.parse("/demo"), materialise=False)
        assert target.path.pipeline == "demo"
        assert target.address_name == "d"
