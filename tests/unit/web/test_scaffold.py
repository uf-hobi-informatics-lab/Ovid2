"""Phase 3 — web scaffold endpoints + pipeline-create page.

Exercises ``GET /api/v1/pipeline-templates``,
``POST /api/v1/pipeline-packages/scaffold``, and the
``/pipeline-packages/new`` page
through a live FastAPI ``TestClient``. Scaffolding is redirected to a
temp pipelines root via a monkeypatch so the real ``pipelines/`` tree is
never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def temp_pipelines_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the scaffolder at a throwaway pipelines directory."""
    root = tmp_path / "pipelines_out"
    root.mkdir()
    monkeypatch.setattr(
        "ovid2.tools.new_pipeline._pipelines_root", lambda: root
    )
    return root


@pytest.mark.unit
def test_list_pipeline_templates(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/pipeline-templates")
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()}
    assert "pcornet_etl" in ids
    for t in r.json():
        assert t["kind"] == "etl"
        assert t["source_pipeline"]
        assert t["cdm"]


@pytest.mark.unit
def test_scaffold_via_api(web_client, temp_pipelines_root: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/api/v1/pipeline-packages/scaffold",
        json={"name": "apihosp", "template": "pcornet_etl"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "apihosp"
    assert body["source_pipeline"] == "example_pcornet"
    assert body["cdm"] == "pcornet_7_0"
    assert (temp_pipelines_root / "apihosp" / "pipeline_settings.py").is_file()


@pytest.mark.unit
def test_scaffold_unknown_template_400(web_client, temp_pipelines_root: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/api/v1/pipeline-packages/scaffold",
        json={"name": "x", "template": "nope"},
    )
    assert r.status_code == 400
    assert "Unknown pipeline template" in r.json()["detail"]


@pytest.mark.unit
def test_scaffold_duplicate_400(web_client, temp_pipelines_root: Path) -> None:  # type: ignore[no-untyped-def]
    web_client.post(
        "/api/v1/pipeline-packages/scaffold",
        json={"name": "dup", "template": "pcornet_etl"},
    )
    r = web_client.post(
        "/api/v1/pipeline-packages/scaffold",
        json={"name": "dup", "template": "pcornet_etl"},
    )
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


@pytest.mark.unit
def test_scaffold_requires_template_or_source(web_client, temp_pipelines_root: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/api/v1/pipeline-packages/scaffold",
        json={"name": "x"},
    )
    assert r.status_code == 400
    assert "template" in r.json()["detail"]


@pytest.mark.unit
def test_pipeline_new_page_lists_templates(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipeline-packages/new")
    assert r.status_code == 200
    body = r.text
    assert "New pipeline" in body
    assert "PCORnet ETL" in body
    # Links to the direct editor for non-ETL kinds.
    assert "/pipelines/new" in body


@pytest.mark.unit
def test_pipeline_new_post_scaffolds_and_redirects(
    web_client, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/pipeline-packages/new",
        data={"name": "formhosp", "template": "pcornet_etl"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc == "/pipeline-packages/formhosp"
    assert (temp_pipelines_root / "formhosp").is_dir()


@pytest.mark.unit
def test_pipeline_new_post_error_preserves_name(
    web_client, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """A scaffold failure re-renders the form (400) with the name kept.

    Omitting the template is a validation error; the operator's typed
    name must survive instead of being wiped by a redirect to a blank
    form.
    """
    r = web_client.post(
        "/pipeline-packages/new",
        data={"name": "keepname", "template": ""},
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    assert 'value="keepname"' in body
