"""Unit tests for the engines REST + HTML routes."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
def test_get_engine_types(web_client, isolated_xdg: Path) -> None:
    r = web_client.get("/api/v1/engines/types")
    assert r.status_code == 200
    data = r.json()
    types = {t["type"] for t in data}
    assert types == {"pyspark", "databricks_spark", "snowpark"}


@pytest.mark.unit
def test_list_empty(web_client, isolated_xdg: Path) -> None:
    r = web_client.get("/api/v1/engines")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.unit
def test_create_get_update_delete(web_client, isolated_xdg: Path) -> None:
    payload = {
        "type": "pyspark",
        "args": {"spark.executor.memory": "64g"},
        "secrets": {},
    }
    # create
    r = web_client.post("/api/v1/engines/pyspark_prod", json=payload)
    assert r.status_code == 201
    # get
    r = web_client.get("/api/v1/engines/pyspark_prod")
    assert r.status_code == 200
    detail = r.json()
    assert detail["type"] == "pyspark"
    assert detail["args"]["spark.executor.memory"] == "64g"
    # update
    payload2 = dict(payload)
    payload2["args"] = {"spark.executor.memory": "72g"}
    r = web_client.put("/api/v1/engines/pyspark_prod", json=payload2)
    assert r.status_code == 200
    r = web_client.get("/api/v1/engines/pyspark_prod")
    assert r.json()["args"]["spark.executor.memory"] == "72g"
    # delete is gated: approve, then delete.
    r = web_client.delete("/api/v1/engines/pyspark_prod")
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    web_client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "test"}
    )
    r = web_client.delete("/api/v1/engines/pyspark_prod")
    assert r.status_code == 200
    # 404 after delete
    r = web_client.get("/api/v1/engines/pyspark_prod")
    assert r.status_code == 404


@pytest.mark.unit
def test_create_conflict_returns_400(web_client, isolated_xdg: Path) -> None:
    payload = {"type": "pyspark", "args": {}, "secrets": {}}
    web_client.post("/api/v1/engines/dup", json=payload)
    r = web_client.post("/api/v1/engines/dup", json=payload)
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


@pytest.mark.unit
def test_invalid_type_rejected(web_client, isolated_xdg: Path) -> None:
    r = web_client.post(
        "/api/v1/engines/bogus",
        json={"type": "no_such_engine", "args": {}, "secrets": {}},
    )
    assert r.status_code == 400


@pytest.mark.unit
def test_databricks_requires_workspace_url(web_client, isolated_xdg: Path) -> None:
    r = web_client.post(
        "/api/v1/engines/db_bad",
        json={"type": "databricks_spark", "args": {}, "secrets": {"token": "x"}},
    )
    assert r.status_code == 400
    assert "workspace_url" in r.json()["detail"]


@pytest.mark.unit
def test_secrets_are_masked_on_get(web_client, isolated_xdg: Path) -> None:
    web_client.post(
        "/api/v1/engines/db_masked",
        json={
            "type": "databricks_spark",
            "args": {"workspace_url": "https://a"},
            "secrets": {"token": "REAL_SECRET_VAL"},
        },
    )
    r = web_client.get("/api/v1/engines/db_masked")
    detail = r.json()
    # The secret name is exposed but the value is the sentinel.
    assert detail["secrets"] == {"token": "***"}
    # And the raw response bytes must not contain the real value.
    assert "REAL_SECRET_VAL" not in r.text


@pytest.mark.unit
def test_html_list_page_renders(web_client, isolated_xdg: Path) -> None:
    web_client.post(
        "/api/v1/engines/render_test",
        json={"type": "snowpark", "args": {"account": "a", "user": "u"},
              "secrets": {"password": "p"}},
    )
    r = web_client.get("/engines")
    assert r.status_code == 200
    assert "render_test" in r.text
    assert "snowpark" in r.text


@pytest.mark.unit
def test_html_new_form_switches_by_type(web_client, isolated_xdg: Path) -> None:
    r = web_client.get("/engines/new?type=snowpark")
    assert r.status_code == 200
    # Snowpark's account field must be in the form.
    assert "account" in r.text
    assert "warehouse" in r.text

    r = web_client.get("/engines/new?type=pyspark")
    assert r.status_code == 200
    assert "spark.executor.memory" in r.text


@pytest.mark.unit
def test_html_new_form_defaults_to_pyspark_for_unknown(
    web_client, isolated_xdg: Path
) -> None:
    r = web_client.get("/engines/new?type=made_up")
    assert r.status_code == 200
    assert "spark.executor.memory" in r.text  # pyspark defaults


@pytest.mark.unit
def test_html_edit_form_requires_existing(web_client, isolated_xdg: Path) -> None:
    r = web_client.get("/engines/nope/edit")
    assert r.status_code == 404


@pytest.mark.unit
def test_html_detail_shows_masked_secret(web_client, isolated_xdg: Path) -> None:
    web_client.post(
        "/api/v1/engines/detail_test",
        json={
            "type": "databricks_spark",
            "args": {"workspace_url": "https://a"},
            "secrets": {"token": "VERY_SECRET"},
        },
    )
    r = web_client.get("/engines/detail_test")
    assert r.status_code == 200
    assert "***" in r.text
    assert "VERY_SECRET" not in r.text
