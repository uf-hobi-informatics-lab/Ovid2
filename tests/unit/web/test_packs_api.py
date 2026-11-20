"""Unit tests — domain-pack web surface (JSON API + HTML pages + repair context)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def acme_enabled(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable a folder pack ``acme`` (CDM + 1 check + 1 package)."""
    from ovid2.packs import clear_cache
    from tests.unit.packs.conftest import make_pack

    root = isolated_xdg / "packs"
    root.mkdir()
    make_pack(root, "acme")
    monkeypatch.setenv("OVID2_PACKS_DIR", str(root))
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    yield root
    clear_cache()


def test_api_list_packs(acme_enabled: Path, web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/packs")
    assert r.status_code == 200, r.text
    packs = {p["id"]: p for p in r.json()}
    assert packs["pcornet_7_0"]["origin"] == "builtin"
    assert packs["pcornet_7_0"]["check_count"] >= 36
    assert packs["acme"]["active"] is True
    assert packs["acme"]["check_count"] == 1 and packs["acme"]["package_count"] == 1


def test_api_get_pack(acme_enabled: Path, web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/packs/acme")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checks"] == ["acme.subject_present"]
    assert body["packages"] == ["acme.baseline"]
    assert web_client.get("/api/v1/packs/nope").status_code == 404


def test_html_pages(acme_enabled: Path, web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/packs")
    assert r.status_code == 200, r.text
    assert 'data-pack-row="pcornet_7_0"' in r.text and 'data-pack-row="acme"' in r.text
    d = web_client.get("/packs/acme")
    assert d.status_code == 200, d.text
    assert 'data-check-row="acme.subject_present"' in d.text
    assert web_client.get("/packs/nope").status_code == 404


def _write_report(instance_id: str) -> None:
    from ovid2.web.services import health_report_svc

    path = health_report_svc.report_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "pipeline": "demo",
                "cdm": "pcornet_7_0",
                "checks": [
                    {
                        "id": "health.null_rate",
                        "status": "exception",
                        "exception_count": 2,
                        "metrics": [
                            {
                                "source_table": "DEMOGRAPHIC",
                                "metric": "NULL_PCT",
                                "value_num": 99.0,
                                "exception_flag": 1,
                                "value_str": "ROWVALUE",
                            }
                        ],
                    },
                    {"id": "1.01", "status": "passed"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_repair_context_endpoint(isolated_xdg: Path, web_client) -> None:  # type: ignore[no-untyped-def]
    iid = "20260101-000000-abcdef"
    assert web_client.get(f"/api/v1/runs/{iid}/repair-context").status_code == 404
    _write_report(iid)
    r = web_client.get(f"/api/v1/runs/{iid}/repair-context")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["check_id"] for f in body["failures"]] == ["health.null_rate"]
    assert body["failures"][0]["remediation"]["transforms"] == ["standardize_null"]


def test_repair_context_service_redacts(isolated_xdg: Path) -> None:
    from ovid2.web.services import health_report_svc

    iid = "20260101-000000-abcdee"
    _write_report(iid)
    redacted = health_report_svc.repair_context(iid, include_row_values=False)
    assert redacted is not None and "ROWVALUE" not in json.dumps(redacted)
    full = health_report_svc.repair_context(iid, include_row_values=True)
    assert full is not None and "ROWVALUE" in json.dumps(full)


def test_repair_context_redacts_for_agent_token(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth on: the MCP service token (an agent) never sees row values; an operator does."""
    import hashlib
    import os

    from fastapi.testclient import TestClient

    from ovid2.web import db as web_db
    from ovid2.web.app import create_app
    from ovid2.web.auth import service_token
    from ovid2.web.models import GlobalSettings, SettingsAuth
    from ovid2.web.services import settings_svc

    web_db.ensure_schema()
    settings = GlobalSettings(
        auth=SettingsAuth(enabled=True, service_principal_role="pipeline_operator")
    )
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    secrets = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    secrets.mkdir(parents=True, exist_ok=True)
    token_file = secrets / f"{service_token.SECRET_NAME}.json"
    token_file.write_text(
        json.dumps({hashlib.sha256(b"op-token").hexdigest(): "pipeline_operator"}),
        encoding="utf-8",
    )
    token_file.chmod(0o600)
    iid = "20260101-000000-abcdef"
    _write_report(iid)
    with TestClient(create_app()) as client:
        url = f"/api/v1/runs/{iid}/repair-context"
        assert client.get(url).status_code == 401
        r = client.get(url, headers={"Authorization": "Bearer op-token"})
        assert r.status_code == 200, r.text
        assert r.json()["row_values_included"] is False
        assert "ROWVALUE" not in r.text
