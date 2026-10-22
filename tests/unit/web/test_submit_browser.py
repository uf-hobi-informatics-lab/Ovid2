"""Redesigned submit page + storage browser widget + browse-mode submit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_disc_connection(xdg: Path, name: str, host_path: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": host_path,
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        )
    )


@pytest.mark.unit
def test_submit_page_says_submit_a_job(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/submit")
    assert r.status_code == 200
    body = r.text
    assert "Submit a job" in body
    assert "Pipeline type" in body
    # Default kind is etl. The kind <select> now renders human labels
    # with a description tooltip (run-kind registry), so the option
    # carries a ``title`` attribute between the value and ``selected``.
    assert 'value="etl"' in body
    assert 'title=' in body and "selected" in body
    # The registry label (not the bare name) is shown for etl.
    assert ">ETL<" in body
    # New sections are present.
    assert 'id="tables-card"' in body
    assert 'id="transformations-card"' in body
    assert "Advanced options" in body


@pytest.mark.unit
def test_submit_page_storage_cards_and_browser(
    web_client, isolated_xdg: Path, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "local_disc", str(root))
    r = web_client.get("/submit")
    assert r.status_code == 200
    body = r.text
    assert "/static/browse.js" in body
    assert "/static/submit_plan.js" in body
    assert 'data-storage-card="input"' in body
    assert 'data-storage-card="output"' in body
    assert 'data-storage-picker="input"' in body
    assert 'data-storage-picker="output"' in body
    # The storage account appears in the picker <select>.
    assert "local_disc" in body
    # Mode toggle present.
    assert "mode-btn" in body


@pytest.mark.unit
def test_browse_js_served(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/static/browse.js")
    assert r.status_code == 200
    assert "data-storage-picker" in r.text
    assert "/api/v1/storage/" in r.text


@pytest.mark.unit
def test_submit_plan_js_served(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/static/submit_plan.js")
    assert r.status_code == 200
    assert "/api/v1/pipelines/" in r.text
    assert "tables-field" in r.text
    assert "transformations-field" in r.text
