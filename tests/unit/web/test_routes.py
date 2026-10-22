"""End-to-end route tests using :class:`fastapi.testclient.TestClient`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.unit
def test_dashboard_renders(web_client: object) -> None:
    """The dashboard (now at /dashboard) returns HTML with the expected nav links."""
    response = web_client.get("/dashboard")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert "Ovid" in body
    assert "/runs" in body
    assert "Recent runs" in body


@pytest.mark.unit
def test_health_json_endpoint(web_client: object) -> None:
    """The JSON health endpoint returns a valid HealthResponse."""
    response = web_client.get("/api/v1/health")  # type: ignore[attr-defined]
    assert response.status_code == 200
    payload = response.json()
    assert "worst" in payload
    assert "checks" in payload
    assert isinstance(payload["checks"], list)


@pytest.mark.unit
def test_addresses_json_endpoint(web_client: object, sample_address: Path) -> None:
    """The JSON addresses endpoint enumerates the test config."""
    response = web_client.get("/api/v1/addresses")  # type: ignore[attr-defined]
    assert response.status_code == 200
    names = {a["name"] for a in response.json()}
    assert "test_disc" in names


@pytest.mark.unit
def test_addresses_json_detail_masks(web_client: object, isolated_xdg: Path) -> None:
    """Detail endpoint masks the ``password`` field."""
    from ovid2.config import paths as xdg_paths

    configs = xdg_paths.configs_dir()
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "sf1.json").write_text(
        json.dumps(
            {
                "src_name": "snowflake",
                "src_args": {"account": "a", "user": "u", "password": "secret"},
            }
        ),
        encoding="utf-8",
    )
    response = web_client.get("/api/v1/addresses/sf1")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.json()
    assert body["src_args"]["password"] == "***"


@pytest.mark.unit
def test_addresses_json_404(web_client: object) -> None:
    """Unknown address returns 404."""
    response = web_client.get("/api/v1/addresses/nope")  # type: ignore[attr-defined]
    assert response.status_code == 404


@pytest.mark.unit
def test_runs_json_empty(web_client: object) -> None:
    """Fresh install: empty runs list."""
    response = web_client.get("/api/v1/runs")  # type: ignore[attr-defined]
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["runs"] == []


@pytest.mark.unit
def test_run_events_missing_log(web_client: object) -> None:
    """Requesting events for an unknown run returns an empty page."""
    response = web_client.get("/api/v1/runs/missing/events")  # type: ignore[attr-defined]
    assert response.status_code == 200
    payload = response.json()
    assert payload["events"] == []
    assert payload["finished"] is False


@pytest.mark.unit
def test_openapi_shape(web_client: object) -> None:
    """OpenAPI reports every W1 endpoint under ``/api/v1``."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = set(schema["paths"].keys())
    expected = {
        "/api/v1/health",
        "/api/v1/addresses",
        "/api/v1/addresses/{name}",
        "/api/v1/runs",
        "/api/v1/runs/{batch_id}",
        "/api/v1/runs/{instance_id}/events",
    }
    assert expected.issubset(paths), f"missing endpoints: {expected - paths}"


@pytest.mark.unit
def test_html_health_page(web_client: object) -> None:
    """HTML health page renders with the check table."""
    response = web_client.get("/health")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "Environment diagnostics" in response.text


@pytest.mark.unit
def test_html_runs_page(web_client: object) -> None:
    """HTML runs page renders (empty state)."""
    response = web_client.get("/runs")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "Jobs" in response.text
