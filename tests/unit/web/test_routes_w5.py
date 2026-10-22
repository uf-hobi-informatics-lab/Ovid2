"""End-to-end route tests for the install-wizard endpoints (W5)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_install_status(web_client: object, isolated_xdg: Path) -> None:
    """``GET /api/v1/install/status`` returns the ordered step list."""
    r = web_client.get("/api/v1/install/status")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.json()
    keys = [s["key"] for s in body["steps"]]
    assert keys[0] == "bootstrap"
    assert keys[-1] == "done"
    assert isinstance(body["complete"], bool)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_bootstrap_creates_xdg_tree(web_client: object, isolated_xdg: Path) -> None:
    """``POST /api/v1/install/bootstrap`` creates the XDG tree."""
    r = web_client.post("/api/v1/install/bootstrap", json={})  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["log"], list)
    assert any("snowflake.json" in line for line in body["log"])

    # Post-bootstrap the status flips ``bootstrap`` to done.
    status = web_client.get("/api/v1/install/status").json()  # type: ignore[attr-defined]
    bootstrap_step = next(s for s in status["steps"] if s["key"] == "bootstrap")
    assert bootstrap_step["status"] == "done"


# ---------------------------------------------------------------------------
# Snowflake secret
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_snowflake_save_rejects_incomplete_payload(
    web_client: object, isolated_xdg: Path
) -> None:
    """``POST /api/v1/install/snowflake`` rejects payloads missing required keys."""
    web_client.post("/api/v1/install/bootstrap", json={})  # type: ignore[attr-defined]
    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/snowflake",
        json={"values": {"account": "a"}},
    )
    assert r.status_code == 400


@pytest.mark.unit
def test_api_snowflake_save_writes_secret(web_client: object, isolated_xdg: Path) -> None:
    """``POST /api/v1/install/snowflake`` persists a complete payload."""
    web_client.post("/api/v1/install/bootstrap", json={})  # type: ignore[attr-defined]
    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/snowflake",
        json={
            "values": {
                "account": "a",
                "user": "u",
                "password": "p",
                "role": "r",
                "warehouse": "w",
                "database": "d",
                "schema": "s",
            }
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] in {"created", "updated"}


@pytest.mark.unit
def test_api_snowflake_password_never_returned(web_client: object, isolated_xdg: Path) -> None:
    """After saving, the password is not exposed by any read endpoint."""
    web_client.post("/api/v1/install/bootstrap", json={})  # type: ignore[attr-defined]
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/snowflake",
        json={
            "values": {
                "account": "a",
                "user": "u",
                "password": "top-secret-value",
                "role": "r",
                "warehouse": "w",
                "database": "d",
                "schema": "s",
            }
        },
    )
    status = web_client.get("/api/v1/install/status").text  # type: ignore[attr-defined]
    assert "top-secret-value" not in status
    secret_shape = web_client.get("/api/v1/secrets/snowflake").text  # type: ignore[attr-defined]
    assert "top-secret-value" not in secret_shape


# ---------------------------------------------------------------------------
# Service command
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_service_command_defaults(web_client: object, isolated_xdg: Path) -> None:
    """``GET /api/v1/install/service/command`` returns a sudo invocation."""
    r = web_client.get("/api/v1/install/service/command")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.json()
    assert body["command"].startswith("sudo ovid2 service install --user ")
    assert body["user"]
    assert body["ovid2_bin"]


@pytest.mark.unit
def test_api_service_command_override(web_client: object, isolated_xdg: Path) -> None:
    """The service command honours the ``user`` / ``ovid2_bin`` overrides."""
    r = web_client.get(  # type: ignore[attr-defined]
        "/api/v1/install/service/command",
        params={"user": "ops", "ovid2_bin": "/usr/local/bin/ovid2"},
    )
    body = r.json()
    assert body["user"] == "ops"
    assert body["ovid2_bin"] == "/usr/local/bin/ovid2"
    assert "--user ops" in body["command"]
    assert "--bin /usr/local/bin/ovid2" in body["command"]


# ---------------------------------------------------------------------------
# Pipeline verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_pipeline_verify_missing(web_client: object, isolated_xdg: Path) -> None:
    """Verifying a non-existent pipeline returns 404."""
    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/pipeline/verify",
        params={"name": "nonexistent_pipeline_xyz"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Image builds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_image_build_records_row(
    web_client: object,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``POST /api/v1/install/image/build`` inserts a build row."""
    from ovid2.web.services import install_svc

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: "/bin/true")

    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/image/build",
        json={
            "tag": "test-image:latest",
            "context": str(tmp_path),
            "dockerfile": str(dockerfile),
        },
    )
    assert r.status_code == 201, r.text
    build_id = r.json()["build_id"]

    # Wait for the thread to complete.
    for _ in range(50):
        detail = web_client.get(  # type: ignore[attr-defined]
            f"/api/v1/install/image/builds/{build_id}"
        ).json()
        if detail["status"] != "running":
            break
        time.sleep(0.05)

    listing = web_client.get("/api/v1/install/image/builds").json()  # type: ignore[attr-defined]
    assert listing["total"] >= 1
    build = next(b for b in listing["builds"] if b["build_id"] == build_id)
    assert build["tag"] == "test-image:latest"


@pytest.mark.unit
def test_api_image_build_log_endpoint(
    web_client: object,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``GET /api/v1/install/image/builds/{id}/log`` returns text/plain."""
    from ovid2.web.services import install_svc

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: "/bin/true")

    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/install/image/build",
        json={"context": str(tmp_path), "dockerfile": str(dockerfile)},
    )
    build_id = r.json()["build_id"]
    time.sleep(0.2)
    log = web_client.get(  # type: ignore[attr-defined]
        f"/api/v1/install/image/builds/{build_id}/log"
    )
    assert log.status_code == 200
    assert log.headers["content-type"].startswith("text/plain")


@pytest.mark.unit
def test_api_image_build_detail_404(web_client: object, isolated_xdg: Path) -> None:
    """Unknown build IDs return 404 from the detail and log endpoints."""
    r = web_client.get("/api/v1/install/image/builds/does-not-exist")  # type: ignore[attr-defined]
    assert r.status_code == 404
    r = web_client.get("/api/v1/install/image/builds/does-not-exist/log")  # type: ignore[attr-defined]
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# HTML wizard pages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_html_install_page(web_client: object, isolated_xdg: Path) -> None:
    """``GET /install`` renders the wizard checklist."""
    r = web_client.get("/install")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.text
    assert "Install wizard" in body
    assert "Bootstrap XDG tree" in body
    assert "Snowflake credentials" in body


@pytest.mark.unit
def test_html_install_bootstrap_redirect(web_client: object, isolated_xdg: Path) -> None:
    """``POST /install/bootstrap`` redirects back to the wizard with a flash."""
    r = web_client.post("/install/bootstrap", follow_redirects=False)  # type: ignore[attr-defined]
    assert r.status_code == 303
    assert r.headers["location"].startswith("/install?flash=")


@pytest.mark.unit
def test_html_install_snowflake_form(web_client: object, isolated_xdg: Path) -> None:
    """``POST /install/snowflake`` accepts the wizard's form fields."""
    web_client.post("/install/bootstrap", follow_redirects=False)  # type: ignore[attr-defined]
    form: dict[str, str] = {}
    for i, key in enumerate(
        ("account", "user", "password", "role", "warehouse", "database", "schema")
    ):
        form[f"arg_key_{i}"] = key
        form[f"arg_val_{i}"] = f"val-{key}"
    r = web_client.post(  # type: ignore[attr-defined]
        "/install/snowflake", data=form, follow_redirects=False
    )
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]


@pytest.mark.unit
def test_html_pipeline_verify_form(web_client: object, isolated_xdg: Path) -> None:
    """``POST /install/pipeline/verify`` redirects with a flash on unknown pipeline."""
    r = web_client.post(  # type: ignore[attr-defined]
        "/install/pipeline/verify",
        data={"name": "nonexistent_pipeline_xyz"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_openapi_lists_w5_endpoints(web_client: object) -> None:
    """OpenAPI reports every W5 endpoint under ``/api/v1/install``."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = set(schema["paths"].keys())
    expected = {
        "/api/v1/install/status",
        "/api/v1/install/bootstrap",
        "/api/v1/install/snowflake",
        "/api/v1/install/image/build",
        "/api/v1/install/image/builds",
        "/api/v1/install/image/builds/{build_id}",
        "/api/v1/install/image/builds/{build_id}/log",
        "/api/v1/install/service/command",
        "/api/v1/install/pipeline/verify",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"
