"""HTTP-level tests for the W2 pipeline / formatter / mapper routes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture()
def pipeline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set up a fake pipeline package for the route tests."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_routes"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\nDEFAULT_PIPELINE_DB_NAME = 'TP_ROUTES_DB'\n",
        encoding="utf-8",
    )
    (pkg / "formatters" / "demographic.yaml").write_text(
        'v: 1\nformatter: demographic\nadapter: source_passthrough\n'
        'sources:\n  demographic:\n    glob: "*Demographic*"\n'
        'projections:\n  PATID: "patid"\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_routes"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


@pytest.mark.unit
def test_pipelines_list_html(web_client: object, pipeline_env: str) -> None:
    """The pipeline-package roster page includes the fake pipeline name."""
    response = web_client.get("/pipeline-packages")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert pipeline_env in response.text


@pytest.mark.unit
def test_pipelines_list_api(web_client: object, pipeline_env: str) -> None:
    """/api/v1/pipeline-packages returns a JSON summary."""
    response = web_client.get("/api/v1/pipeline-packages")  # type: ignore[attr-defined]
    assert response.status_code == 200
    payload = response.json()
    names = {p["name"] for p in payload}
    assert pipeline_env in names


@pytest.mark.unit
def test_pipeline_detail_html(web_client: object, pipeline_env: str) -> None:
    """The pipeline-package detail page shows the mapper type."""
    response = web_client.get(f"/pipeline-packages/{pipeline_env}")  # type: ignore[attr-defined]
    assert response.status_code == 200
    assert "pcornet_mappers" in response.text
    assert "TP_ROUTES_DB" in response.text


@pytest.mark.unit
def test_pipeline_detail_404(web_client: object) -> None:
    """Unknown pipelines return 404."""
    response = web_client.get("/pipeline-packages/no_such_pipeline_xyz")  # type: ignore[attr-defined]
    assert response.status_code == 404


@pytest.mark.unit
def test_pipeline_file_preview(web_client: object, pipeline_env: str) -> None:
    """The file-preview endpoint returns pipeline_settings.py contents."""
    response = web_client.get(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_env}/file",
        params={"relative_path": "pipeline_settings.py"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "mappers_type" in body["content"]


@pytest.mark.unit
def test_pipeline_file_traversal_rejected(web_client: object, pipeline_env: str) -> None:
    """Path traversal is blocked."""
    response = web_client.get(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_env}/file",
        params={"relative_path": "../../etc/passwd"},
    )
    assert response.status_code == 404


@pytest.mark.unit
def test_formatter_detail_api(web_client: object, pipeline_env: str) -> None:
    """/api/v1/pipeline-packages/.../formatters/{table} returns YAML + parsed."""
    response = web_client.get(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_env}/formatters/demographic"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["adapter"] == "source_passthrough"
    assert body["parsed"]["formatter"] == "demographic"


@pytest.mark.unit
def test_formatter_validate_api(web_client: object, pipeline_env: str) -> None:
    """Validation endpoint returns one entry per file."""
    response = web_client.post(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_env}/formatters:validate"
    )
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert entries[0]["status"] == "ok"


@pytest.mark.unit
def test_formatter_detail_html(web_client: object, pipeline_env: str) -> None:
    """The HTML formatter detail page renders the YAML source."""
    response = web_client.get(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_env}/formatters/demographic"
    )
    assert response.status_code == 200
    assert "source_passthrough" in response.text


@pytest.mark.unit
def test_run_timeline_endpoint_missing_returns_empty(web_client: object) -> None:
    """Missing event log yields an empty timeline (not 404)."""
    response = web_client.get("/api/v1/runs/nope/timeline")  # type: ignore[attr-defined]
    assert response.status_code == 200
    payload = response.json()
    assert payload["events_seen"] == 0
    assert payload["tables"] == []


@pytest.mark.unit
def test_run_events_log_404(web_client: object) -> None:
    """Raw log endpoint returns 404 when the JSONL is missing."""
    response = web_client.get("/api/v1/runs/nope/events.log")  # type: ignore[attr-defined]
    assert response.status_code == 404
