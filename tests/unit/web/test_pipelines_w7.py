"""HTTP-level tests for the W7 endpoints (connections, routes, settings)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.unit
def test_list_connection_types(web_client) -> None:  # type: ignore[no-untyped-def]
    response = web_client.get("/api/v1/connections/types")
    assert response.status_code == 200
    types = {item["type"] for item in response.json()}
    assert {"disc", "snowflake", "s3", "azure_blob"} <= types


@pytest.mark.unit
def test_create_and_get_connection_via_api(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    response = web_client.post(
        "/api/v1/connections/c_disc",
        json={"type": "disc", "args": {"host_path": str(isolated_xdg)}, "secrets": {}},
    )
    assert response.status_code == 201, response.text

    listing = web_client.get("/api/v1/connections")
    assert listing.status_code == 200
    names = [c["name"] for c in listing.json()]
    assert "c_disc" in names

    detail = web_client.get("/api/v1/connections/c_disc")
    assert detail.status_code == 200
    body = detail.json()
    assert body["type"] == "disc"
    assert body["args"]["host_path"] == str(isolated_xdg)
    assert body["secrets"] == {}


@pytest.mark.unit
def test_create_snowflake_hides_password(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    response = web_client.post(
        "/api/v1/connections/c_sf",
        json={
            "type": "snowflake",
            "args": {"account": "a1", "user": "u1"},
            "secrets": {"password": "hunter2"},
        },
    )
    assert response.status_code == 201, response.text
    detail = web_client.get("/api/v1/connections/c_sf")
    body = detail.json()
    assert body["secrets"] == {"password": "***"}
    # The connection file itself never carries the password.
    path = Path(body["path"])
    assert "hunter2" not in path.read_text()


@pytest.mark.unit
def test_delete_connection_via_api(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    web_client.post(
        "/api/v1/connections/c_del",
        json={"type": "disc", "args": {"host_path": str(isolated_xdg)}, "secrets": {}},
    )
    # Delete is gated: first attempt awaits approval, then removes.
    r = web_client.delete("/api/v1/connections/c_del")
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    web_client.post(f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "test"})
    r = web_client.delete("/api/v1/connections/c_del")
    assert r.status_code == 200
    assert web_client.get("/api/v1/connections/c_del").status_code == 404


@pytest.mark.unit
def test_test_probe_via_api(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    web_client.post(
        "/api/v1/connections/c_probe",
        json={"type": "disc", "args": {"host_path": str(isolated_xdg)}, "secrets": {}},
    )
    r = web_client.post("/api/v1/connections/c_probe/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["code"] == "ok"


@pytest.mark.unit
def test_connections_html_index(web_client) -> None:  # type: ignore[no-untyped-def]
    # /connections now redirects to the storage-accounts index.
    r = web_client.get("/connections")
    assert r.status_code == 200
    assert "Storage accounts" in r.text


@pytest.mark.unit
def test_storage_html_index(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/storage")
    assert r.status_code == 200
    assert "Storage accounts" in r.text


@pytest.mark.unit
def test_connections_new_form_switches_fields(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/connections/new?type=snowflake")
    assert r.status_code == 200
    assert "account" in r.text
    assert "warehouse" in r.text
    assert "password" in r.text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def routes_env(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Preload one pipeline + two disc addresses so route writes succeed."""
    monkeypatch.setattr(
        "ovid2.web.services.pipelines_svc.pipelines_meta_svc.discover_pipelines",
        lambda: ["ptr"],
    )
    monkeypatch.setattr(
        "ovid2.web.services.pipelines_svc.list_pipelines",
        lambda: [],
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "in.json").write_text('{"src_name": "disc", "src_args": {"path": "/tmp/in"}}')
    (configs / "out.json").write_text('{"src_name": "disc", "src_args": {"path": "/tmp/out"}}')
    return isolated_xdg


def _base_route_payload() -> dict[str, object]:
    """Return a JSON dict that PUT /api/v1/pipelines/... will accept."""
    return {
        "pipeline": "ptr",
        "input_address": "in",
        "output_address": "out",
        "tables": ["all"],
        "transformations": ["all"],
        "engine": "pyspark",
        "workers": 4,
        "enabled": True,
        "trigger": {"type": "manual"},
    }


@pytest.mark.unit
def test_route_crud_via_api(web_client, routes_env: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post("/api/v1/pipelines/r1", json=_base_route_payload())
    assert r.status_code == 201, r.text
    assert web_client.get("/api/v1/pipelines/r1").status_code == 200
    # PUT idempotent update
    upd = _base_route_payload() | {"workers": 6}
    assert web_client.put("/api/v1/pipelines/r1", json=upd).status_code == 200
    detail = web_client.get("/api/v1/pipelines/r1").json()
    assert detail["workers"] == 6
    # DELETE is gated: approve, then delete.
    r = web_client.delete("/api/v1/pipelines/r1")
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    web_client.post(f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "test"})
    assert web_client.delete("/api/v1/pipelines/r1").status_code == 200
    assert web_client.get("/api/v1/pipelines/r1").status_code == 404


@pytest.mark.unit
def test_route_run_now_via_api(web_client, routes_env: Path) -> None:  # type: ignore[no-untyped-def]
    web_client.post("/api/v1/pipelines/r_run", json=_base_route_payload())
    resp = web_client.post("/api/v1/pipelines/r_run/run")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]


@pytest.mark.unit
def test_route_run_now_copy_needs_tables_via_api(web_client, routes_env: Path) -> None:  # type: ignore[no-untyped-def]
    """A copy pipeline open on tables: 400 without an override, 201 with one.

    Regression for the field report — the JSON API run handler now accepts
    an optional ``tables`` body so a ``copy`` pipeline created *open* on
    tables can be run without editing the config.
    """
    payload = _base_route_payload() | {
        "kind": "copy",
        "cdm": None,
        "tables": [],
        "transformations": [],
    }
    r = web_client.post("/api/v1/pipelines/r_copy", json=payload)
    assert r.status_code == 201, r.text
    # No override → submit-time validation rejects the empty list as a 400.
    bad = web_client.post("/api/v1/pipelines/r_copy/run")
    assert bad.status_code == 400, bad.text
    assert "tables" in bad.json()["detail"]
    # Explicit tables (stray ``all`` dropped) → the job submits.
    good = web_client.post(
        "/api/v1/pipelines/r_copy/run",
        json={"tables": ["DEMOGRAPHIC", "all", "ENCOUNTER"]},
    )
    assert good.status_code == 201, good.text
    assert good.json()["status"] == "queued"


@pytest.mark.unit
def test_route_enable_disable_via_api(web_client, routes_env: Path) -> None:  # type: ignore[no-untyped-def]
    payload = _base_route_payload()
    payload["trigger"] = {"type": "cron", "cron": "0 3 * * *"}
    web_client.post("/api/v1/pipelines/r_toggle", json=payload)
    r = web_client.post("/api/v1/pipelines/r_toggle/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    r = web_client.post("/api/v1/pipelines/r_toggle/enable")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.unit
def test_routes_html_index(web_client) -> None:  # type: ignore[no-untyped-def]
    # /routes back-compat 307-redirects to the canonical /pipelines list.
    r = web_client.get("/routes", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/pipelines"


@pytest.mark.unit
def test_pipelines_html_index(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipelines")
    assert r.status_code == 200
    assert "Pipelines" in r.text


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_settings_defaults_via_api(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["defaults"]["engine"] == "pyspark"


@pytest.mark.unit
def test_settings_put_persists(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    current = web_client.get("/api/v1/settings").json()
    current["defaults"]["engine"] = "http-eng"
    r = web_client.put("/api/v1/settings", json=current)
    assert r.status_code == 200, r.text
    reloaded = web_client.get("/api/v1/settings").json()
    assert reloaded["defaults"]["engine"] == "http-eng"


@pytest.mark.unit
def test_settings_html(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/settings")
    assert r.status_code == 200
    assert "Global settings" in r.text
    # HTML POST round-trip works.
    r2 = web_client.post(
        "/settings",
        data={
            "defaults_engine": "html-eng",
            "defaults_workers": "5",
            "worker_poll": "3",
            "route_dispatch_enabled": "on",
            "ui_dash": "1000",
            "ui_run": "500",
            "smtp_host": "",
            "smtp_port": "25",
            "smtp_from": "",
            "smtp_to": "",
            "smtp_secret_ref": "",
            "event_log_days": "10",
            "web_jobs_days": "20",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    body = web_client.get("/api/v1/settings").json()
    assert body["defaults"]["engine"] == "html-eng"
    assert body["retention"]["event_log_days"] == 10
