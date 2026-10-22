"""Tests for the ODS web routes (``/api/v1/dataspace/*``)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def with_disc_address(
    isolated_xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Seed a disc address pointed at a fresh pipeline tree."""
    monkeypatch.chdir(tmp_path)
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    root = tmp_path / "disc"
    (root / "demo" / "20260316").mkdir(parents=True)
    (root / "demo" / "20260316" / "DEMOGRAPHIC.csv").write_text("a\n")
    _write_json(
        configs / "d.json",
        {"src_name": "disc", "src_args": {"path": str(root)}},
    )
    return root


class TestListPipelines:
    def test_lists_pipeline_from_disc_tree(self, web_client, with_disc_address) -> None:
        response = web_client.get("/api/v1/dataspace")
        assert response.status_code == 200
        payload = response.json()
        names = [p["name"] for p in payload["pipelines"]]
        assert "demo" in names


class TestDimensionCatalog:
    def test_returns_registered_dimensions(self, web_client, with_disc_address) -> None:
        response = web_client.get("/api/v1/dataspace/dimensions")
        assert response.status_code == 200
        payload = response.json()
        names = {d["name"] for d in payload["dimensions"]}
        assert {"disc", "snowflake", "s3", "azure_blob"}.issubset(names)
        assert payload["default_dimension"] == "disc"
        disc = next(d for d in payload["dimensions"] if d["name"] == "disc")
        assert disc["address_count"] >= 1


class TestListBatches:
    def test_returns_batches(self, web_client, with_disc_address) -> None:
        response = web_client.get("/api/v1/dataspace/demo")
        assert response.status_code == 200
        payload = response.json()
        assert payload["pipeline"] == "demo"
        assert [b["batch_id"] for b in payload["batches"]] == ["20260316"]


class TestListTables:
    def test_returns_tables(self, web_client, with_disc_address) -> None:
        response = web_client.get("/api/v1/dataspace/demo/20260316")
        assert response.status_code == 200
        payload = response.json()
        assert payload["batch_id"] == "20260316"
        assert [t["name"] for t in payload["tables"]] == ["DEMOGRAPHIC"]


class TestResolve:
    def test_happy_path(self, web_client, with_disc_address) -> None:
        response = web_client.get(
            "/api/v1/dataspace/resolve",
            params={"path": "/demo/20260316/DEMOGRAPHIC"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["pipeline"] == "demo"
        assert payload["table"] == "DEMOGRAPHIC"
        assert payload["dimension"] == "disc"
        assert payload["address_name"] == "d"
        assert payload["tier"] == "sole_candidate"

    def test_bad_path_returns_400(self, web_client, with_disc_address) -> None:
        response = web_client.get(
            "/api/v1/dataspace/resolve",
            params={"path": "not_absolute"},
        )
        assert response.status_code == 400

    def test_unknown_dim(self, web_client, with_disc_address) -> None:
        response = web_client.get(
            "/api/v1/dataspace/resolve",
            params={"path": "/demo", "dim": "nonsense"},
        )
        assert response.status_code == 400
