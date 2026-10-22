"""End-to-end route tests for W4 config CRUD endpoints."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Address REST endpoints
# ---------------------------------------------------------------------------


def _make_disc_connection(web_client: object, name: str = "c_disc") -> None:
    """Create a disc connection so the address POST has something to reference."""
    r = web_client.post(  # type: ignore[attr-defined]
        f"/api/v1/connections/{name}",
        json={"type": "disc", "args": {"host_path": "/tmp"}},
    )
    assert r.status_code in (200, 201), r.text


@pytest.mark.unit
def test_api_create_address(web_client: object, isolated_xdg: Path) -> None:
    """``POST /api/v1/addresses/{name}`` creates a W7 (connection-shaped) config."""
    _make_disc_connection(web_client)
    response = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/addresses/new_disc",
        json={"connection": "c_disc", "location": {"path": "x"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["action"] == "created"
    assert body["path"].endswith("/new_disc.json")


@pytest.mark.unit
def test_api_create_refuses_legacy_shape(
    web_client: object, isolated_xdg: Path
) -> None:
    """After the W7 flip, legacy-shape POSTs return HTTP 400."""
    response = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/addresses/legacy_new",
        json={"src_name": "disc", "src_args": {"path": "/tmp/x"}},
    )
    assert response.status_code == 400
    assert "no longer creatable" in response.text


@pytest.mark.unit
def test_api_put_address_conflict(web_client: object, isolated_xdg: Path) -> None:
    """``POST`` on an existing name returns 400 (create-only)."""
    _make_disc_connection(web_client)
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/addresses/dup",
        json={"connection": "c_disc", "location": {"path": "x"}},
    )
    response = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/addresses/dup",
        json={"connection": "c_disc", "location": {"path": "y"}},
    )
    assert response.status_code == 400


@pytest.mark.unit
def test_api_put_address_upsert(web_client: object, isolated_xdg: Path) -> None:
    """``PUT`` is idempotent: creates then updates the same name."""
    _make_disc_connection(web_client)
    payload = {"connection": "c_disc", "location": {"path": "x"}}
    r1 = web_client.put("/api/v1/addresses/upsert", json=payload)  # type: ignore[attr-defined]
    assert r1.status_code == 200
    assert r1.json()["action"] == "created"
    payload["location"] = {"path": "y"}
    r2 = web_client.put("/api/v1/addresses/upsert", json=payload)  # type: ignore[attr-defined]
    assert r2.status_code == 200
    assert r2.json()["action"] == "updated"


@pytest.mark.unit
def test_api_delete_address(web_client: object, isolated_xdg: Path) -> None:
    """``DELETE`` is gated: first call awaits approval, then removes."""
    _make_disc_connection(web_client)
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/addresses/togo",
        json={"connection": "c_disc", "location": {"path": "x"}},
    )
    # First attempt is gated → 202 with an approval_id.
    r = web_client.delete("/api/v1/addresses/togo")  # type: ignore[attr-defined]
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    # Address still present until approved.
    assert web_client.get("/api/v1/addresses/togo").status_code == 200  # type: ignore[attr-defined]
    # Human approves, then the delete goes through.
    web_client.post(  # type: ignore[attr-defined]
        f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "test"}
    )
    r = web_client.delete("/api/v1/addresses/togo")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert r.json()["action"] == "deleted"
    r404 = web_client.get("/api/v1/addresses/togo")  # type: ignore[attr-defined]
    assert r404.status_code == 404


@pytest.mark.unit
def test_html_address_new_page(web_client: object) -> None:
    """The `/addresses/new` page renders the connection picker.

    After the W7 flip this is now step 1 of 2 — no raw type radio,
    just a list of available connections plus a link to
    ``/connections/new``.
    """
    r = web_client.get("/addresses/new")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert "step 1 of 2" in r.text
    # The picker links back to connection creation.
    assert "/connections/new" in r.text


# ---------------------------------------------------------------------------
# Secret REST endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_create_secret(web_client: object, isolated_xdg: Path) -> None:
    """``POST /api/v1/secrets/{name}`` writes a JSON file with 0o600."""
    r = web_client.post(  # type: ignore[attr-defined]
        "/api/v1/secrets/sf",
        json={"values": {"user": "u", "password": "p"}},
    )
    assert r.status_code == 201, r.text
    path = Path(r.json()["path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.unit
def test_api_list_secrets_never_returns_values(
    web_client: object, isolated_xdg: Path
) -> None:
    """``GET /api/v1/secrets`` returns key names but no values."""
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/secrets/sf",
        json={"values": {"user": "u", "password": "hunter2"}},
    )
    r = web_client.get("/api/v1/secrets")  # type: ignore[attr-defined]
    body_text = r.text
    assert r.status_code == 200
    assert "hunter2" not in body_text
    entries = r.json()
    assert entries[0]["keys"] == ["password", "user"]


@pytest.mark.unit
def test_api_delete_secret(web_client: object, isolated_xdg: Path) -> None:
    """``DELETE /api/v1/secrets/{name}`` is gated then removes the file."""
    web_client.post(  # type: ignore[attr-defined]
        "/api/v1/secrets/bye", json={"values": {"k": "v"}}
    )
    r = web_client.delete("/api/v1/secrets/bye")  # type: ignore[attr-defined]
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    web_client.post(  # type: ignore[attr-defined]
        f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "test"}
    )
    r = web_client.delete("/api/v1/secrets/bye")  # type: ignore[attr-defined]
    assert r.status_code == 200
    r404 = web_client.get("/api/v1/secrets/bye")  # type: ignore[attr-defined]
    assert r404.status_code == 404


@pytest.mark.unit
def test_html_secrets_index(web_client: object) -> None:
    """The `/secrets` index page renders."""
    r = web_client.get("/secrets")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert "Secrets" in r.text


# ---------------------------------------------------------------------------
# Formatter / mapper REST endpoints (require a pipeline package)
# ---------------------------------------------------------------------------


@pytest.fixture()
def pipeline_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_xdg: Path
) -> str:
    """Register a minimal pipeline on ``sys.path`` for the duration of the test."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_route"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "MAPPERS_TYPE = 'pcornet'\nDEFAULT_PIPELINE_DB_NAME = 'tp'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_route"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


_VALID_FORMATTER = (
    "v: 1\n"
    "formatter: demographic\n"
    "adapter: source_passthrough\n"
    'sources:\n  demographic:\n    glob: "*Demographic*"\n'
    "projections:\n  PATID: patid\n"
)


@pytest.mark.unit
def test_api_put_formatter(web_client: object, pipeline_on_disk: str) -> None:
    """PUT writes a formatter YAML after schema validation."""
    r = web_client.put(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_on_disk}/formatters/demographic",
        json={"raw_text": _VALID_FORMATTER},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "created"


@pytest.mark.unit
def test_api_put_formatter_rejects_invalid_schema(
    web_client: object, pipeline_on_disk: str
) -> None:
    """A schema-invalid YAML is refused with 400."""
    r = web_client.put(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_on_disk}/formatters/bad",
        json={"raw_text": "formatter: bad\nadapter: source_passthrough\n"},
    )
    assert r.status_code == 400


@pytest.mark.unit
def test_api_delete_formatter(web_client: object, pipeline_on_disk: str) -> None:
    """DELETE removes a formatter YAML."""
    web_client.put(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_on_disk}/formatters/demographic",
        json={"raw_text": _VALID_FORMATTER},
    )
    r = web_client.delete(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_on_disk}/formatters/demographic"
    )
    assert r.status_code == 200
    assert r.json()["action"] == "deleted"


@pytest.mark.unit
def test_api_put_mapper(web_client: object, pipeline_on_disk: str) -> None:
    """PUT writes a mapper YAML after schema validation."""
    r = web_client.put(  # type: ignore[attr-defined]
        f"/api/v1/pipeline-packages/{pipeline_on_disk}/mappers/demographic",
        json={"raw_text": "v: 1\ntable: demographic\nops: []\n"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "created"


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_openapi_lists_w4_endpoints(web_client: object) -> None:
    """OpenAPI reports every W4 endpoint under ``/api/v1``."""
    schema = web_client.get("/openapi.json").json()  # type: ignore[attr-defined]
    paths = set(schema["paths"].keys())
    expected = {
        "/api/v1/addresses/{name}",
        "/api/v1/secrets",
        "/api/v1/secrets/{name}",
        "/api/v1/pipeline-packages/{pipeline}/formatters/{table}",
        "/api/v1/pipeline-packages/{pipeline}/mappers/{table}",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"
