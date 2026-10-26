"""End-to-end HTTP check: a cancelled run renders dimmed, not bright.

Exercises the real web surface (FastAPI TestClient) to confirm the fix
for the "cancelled job still too bright" report: after cancelling, the
runs and jobs lists must emit the muted ``status-cancelled`` styling and
a ``status-cancelled`` row class — never the bright red ``status-error``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def pipeline_env_run(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a stub pipeline + two disc address configs."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_cancel"
    (pkg / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text("mappers_type='pcornet_mappers'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_c.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/i"}}),
        encoding="utf-8",
    )
    (configs / "out_c.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/o"}}),
        encoding="utf-8",
    )
    yield "tp_cancel"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


@pytest.mark.unit
def test_cancelled_queued_job_renders_dimmed(web_client: object, pipeline_env_run: str) -> None:
    payload = {
        "pipeline": pipeline_env_run,
        "batch_id": "B1",
        "input_address": "in_c",
        "output_address": "out_c",
        "engine": "snowpark",
    }
    resp = web_client.post("/api/v1/runs", json=payload)  # type: ignore[attr-defined]
    assert resp.status_code == 201, resp.text
    job = resp.json()

    # Cancel through the real HTTP surface.
    cancel = web_client.post(f"/jobs/{job['job_id']}/cancel")  # type: ignore[attr-defined]
    assert cancel.status_code in (200, 303), cancel.text

    # Merged runs workspace (formerly the separate jobs + runs lists):
    # the cancelled run's badge + row must carry the muted cancelled
    # class, never the bright red error styling.
    runs_html = web_client.get("/runs").text  # type: ignore[attr-defined]
    assert "status-cancelled" in runs_html
    assert "badge status-error" not in runs_html

    # Legacy /jobs URL redirects into the same workspace.
    legacy = web_client.get("/jobs").text  # type: ignore[attr-defined]
    assert "status-cancelled" in legacy
    assert "badge status-error" not in legacy


@pytest.mark.unit
def test_stylesheet_has_row_dim_and_button_shadow(web_client: object) -> None:
    css = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    assert "tr.status-cancelled td" in css
    assert "box-shadow: var(--shadow-1)" in css


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
