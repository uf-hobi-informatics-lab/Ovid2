"""Route-level tests for the redesigned submit page.

Covers:
- ``GET /api/v1/pipeline-packages/{pipeline}/plan`` (table + stage + fixer plan).
- ``POST /submit`` in ``browse`` mode → resolve-or-create an address and
  queue a job.
- Empty-selection guards on the ETL submit path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def submit_pipeline(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Register a stub pipeline with a YAML formatter + a disc connection."""
    root = tmp_path / "pipelines_root2"
    pkg = root / "ovid2_pipelines" / "tp_submit"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n", encoding="utf-8"
    )
    (pkg / "formatters" / "demographic.yaml").write_text(
        "adapter: source_passthrough\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]

    # A disc storage account to browse.
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "local_disc.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": str(tmp_path / "disc"),
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        ),
        encoding="utf-8",
    )
    yield "tp_submit"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


@pytest.mark.unit
def test_pipeline_plan_endpoint(web_client, submit_pipeline: str) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get(f"/api/v1/pipeline-packages/{submit_pipeline}/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline"] == submit_pipeline
    assert body["cdm"] == "pcornet_7_0"
    assert body["transformations"] == ["format", "deduplicate", "map", "fix"]
    by_name = {t["name"]: t for t in body["tables"]}
    assert by_name["demographic"]["has_formatter"] is True
    assert by_name["demographic"]["source"] == "yaml"


@pytest.mark.unit
def test_pipeline_plan_unknown_404(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/pipeline-packages/no_such/plan")
    assert r.status_code == 404


@pytest.mark.unit
def test_submit_browse_mode_creates_address_and_queues(
    web_client, submit_pipeline: str, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/submit",
        data={
            "kind": "etl",
            "pipeline": submit_pipeline,
            "batch_id": "20260101",
            "input_mode": "browse",
            "input_connection": "local_disc",
            "input_location": "incoming/tp_submit",
            "output_mode": "browse",
            "output_connection": "local_disc",
            "output_location": "out/tp_submit",
            "tables": "demographic",
            "transformations": "all",
            "engine": "pyspark",
            "workers": "5",
        },
        follow_redirects=False,
    )
    # Success → 303 redirect to /runs/{instance_id}.
    assert r.status_code == 303
    assert "/runs/" in r.headers["location"]

    # Two addresses were minted from the browsed folders.
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    assert (configs / "local_disc__incoming_tp_submit.json").is_file()
    assert (configs / "local_disc__out_tp_submit.json").is_file()


@pytest.mark.unit
def test_submit_browse_missing_connection_errors(
    web_client, submit_pipeline: str
) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/submit",
        data={
            "kind": "etl",
            "pipeline": submit_pipeline,
            "batch_id": "20260101",
            "input_mode": "browse",
            "input_connection": "",
            "input_location": "x",
            "output_mode": "address",
            "output_address": "out_test",
            "tables": "all",
            "transformations": "all",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


@pytest.mark.unit
def test_submit_empty_tables_rejected(
    web_client, submit_pipeline: str, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    for nm in ("in_test", "out_test"):
        (configs / f"{nm}.json").write_text(
            json.dumps({"src_name": "disc", "src_args": {"path": "/tmp"}}),
            encoding="utf-8",
        )
    r = web_client.post(
        "/submit",
        data={
            "kind": "etl",
            "pipeline": submit_pipeline,
            "batch_id": "20260101",
            "input_mode": "address",
            "input_address": "in_test",
            "output_mode": "address",
            "output_address": "out_test",
            "tables": "__none__",  # operator unchecked everything
            "transformations": "all",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


@pytest.mark.unit
def test_submit_empty_transformations_rejected(
    web_client, submit_pipeline: str, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    configs = isolated_xdg / "config" / "ovid2" / "configs"
    for nm in ("in_test", "out_test"):
        (configs / f"{nm}.json").write_text(
            json.dumps({"src_name": "disc", "src_args": {"path": "/tmp"}}),
            encoding="utf-8",
        )
    r = web_client.post(
        "/submit",
        data={
            "kind": "etl",
            "pipeline": submit_pipeline,
            "batch_id": "20260101",
            "input_mode": "address",
            "input_address": "in_test",
            "output_mode": "address",
            "output_address": "out_test",
            "tables": "demographic",
            "transformations": "__none__",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


@pytest.mark.unit
def test_submit_page_renders_browse_validation_hooks(
    web_client, submit_pipeline: str
) -> None:  # type: ignore[no-untyped-def]
    """The submit page must ship the per-card ``picker-error`` targets.

    The client-side hard block (``submit_plan.js::bindStorageValidation``)
    writes its inline "pick an account" message into
    ``[data-role=picker-error]`` on the offending Input / Output card. If
    that hook ever disappears from the template the block degrades to the
    old confusing server-side redirect, so guard the markup here.
    """
    r = web_client.get("/submit")
    assert r.status_code == 200
    html = r.text
    # One error target per storage card (input + output).
    assert html.count('data-role="picker-error"') == 2
    # The mode hidden fields the validator reads must be present.
    assert 'name="input_mode"' in html
    assert 'name="output_mode"' in html
    assert 'name="input_connection"' in html
    assert 'name="output_connection"' in html

