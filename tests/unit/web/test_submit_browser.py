"""Job-builder submit page (/submit) + storage navigator + browse-mode submit.

The page is a navigator (collapsible sections + lazy storage trees) on the
left and a job graph on the right. Selection is by clicking an item's
icon — there are no "use" link-buttons anywhere in the web UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web"


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
def test_submit_page_is_job_builder(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/submit")
    assert r.status_code == 200
    body = r.text
    assert "Submit a job" in body
    # Kind chips (radios) with registry labels + description tooltips.
    assert 'name="kind" value="etl" checked' in body
    assert ">ETL<" in body
    assert "title=" in body
    # Navigator sections, in order.
    order = [
        body.index('data-builder-section="pipeline"'),
        body.index('data-builder-section="input"'),
        body.index('data-builder-section="output"'),
        body.index('data-builder-section="options"'),
    ]
    assert order == sorted(order)
    # Options keep the familiar pickers.
    assert 'id="tables-card"' in body
    assert 'id="transformations-card"' in body
    # Right-hand graph + gated submit.
    assert 'id="job-graph"' in body
    assert "job-graph-svg" in body
    assert 'id="builder-submit" disabled' in body
    # Hidden pipeline field drives the POST.
    assert 'name="pipeline" id="pipeline-field"' in body


@pytest.mark.unit
def test_submit_page_plan_url_points_at_real_endpoint(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/submit").text
    assert 'data-plan-url="/api/v1/pipeline-packages/"' in body


@pytest.mark.unit
def test_submit_page_storage_trees(web_client, isolated_xdg: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "local_disc", str(root))
    r = web_client.get("/submit")
    assert r.status_code == 200
    body = r.text
    assert "/static/submit_builder.js" in body
    assert "/static/submit_plan.js" in body
    assert 'data-tree-role="input"' in body
    assert 'data-tree-role="output"' in body
    # The account is a tree node carrying its type for the logo.
    assert re.search(r'data-kind="account" data-conn="local_disc"\s+data-type="disc"', body), (
        "storage account tree node missing"
    )
    assert 'data-logo="disc"' in body
    # Every hidden field of the POST contract exists once per side.
    for role in ("input", "output"):
        for suffix in ("mode", "address", "connection", "location"):
            assert body.count(f'name="{role}_{suffix}"') == 1, (role, suffix)


@pytest.mark.unit
def test_submit_page_preselect_address(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/submit?input_address=in_x&pipeline=foo&kind=copy").text
    assert 'name="input_address" value="in_x"' in body
    assert 'name="pipeline" id="pipeline-field" value="foo"' in body
    assert 'name="kind" value="copy" checked' in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "asset, needles",
    [
        ("browse.js", ["data-storage-picker", "/api/v1/storage/", "data-pick"]),
        ("storage_tree.js", ["/api/v1/storage/", "ovid2:slot-change", "_connection"]),
        ("submit_plan.js", ["/api/v1/pipeline-packages/", "tables-field", "transformations-field"]),
        ("submit_builder.js", ["ovid2:kind-change", "builder-submit"]),
        ("job_graph.js", ["is-done", "ovid2:graph-focus"]),
        ("logos.js", ["snowflake", "databricks", "azure_blob", "s3"]),
    ],
)
def test_builder_assets_served(web_client, asset: str, needles: list[str]) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get(f"/static/{asset}")
    assert r.status_code == 200
    for n in needles:
        assert n in r.text, (asset, n)


@pytest.mark.unit
def test_no_use_link_buttons_in_web_ui() -> None:
    """Selection is by clicking an icon; no "use" link-buttons remain."""
    offenders: list[str] = []
    for path in list((_WEB / "static").glob("*.js")) + list((_WEB / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if "data-use=" in text or re.search(r">\s*use\s*<", text) or "Use this folder" in text:
            offenders.append(path.name)
    assert not offenders, f"'use' link-buttons found in: {offenders}"
