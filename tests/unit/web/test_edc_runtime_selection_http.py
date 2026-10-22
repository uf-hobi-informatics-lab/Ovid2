"""End-to-end HTTP test for the reusable EDC / data-health run.

Verifies (through the running FastAPI app, per the project's
verification policy) that an operator can submit a health run against
*any* database by selecting a storage account + database/schema at run
time — no per-partner ``ovid2_pipelines`` package and no pre-saved
address required. The generic ``pcornet_edc`` label carries the CDM.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.mark.unit
def test_http_submit_edc_run_with_runtime_storage_selection(web_client, isolated_xdg: Path) -> None:
    # A storage account to "browse" — no saved address, no package.
    conndir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conndir / "prod_disc.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {"host_path": "/srv/data", "container_path": "/data"},
            }
        ),
        encoding="utf-8",
    )

    resp = web_client.post(
        "/api/v1/runs",
        json={
            "kind": "health",
            "pipeline": "pcornet_edc",
            "batch_id": "20260101",
            "input_connection": "prod_disc",
            "input_location": "char_site/2026",
            "output_connection": "prod_disc",
            "output_location": "char_site/results",
            "packages": ["20r1"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"

    detail = web_client.get(f"/api/v1/jobs/{body['job_id']}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["pipeline"] == "pcornet_edc"
    # The run-time selection was resolved-or-created into concrete addresses.
    assert d["input_address"] == "prod_disc__char_site_2026"
    assert d["output_address"] == "prod_disc__char_site_results"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via pytest.")
