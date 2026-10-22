"""Web-tier tests for LLM connections + the assistant chat stream (L1).

The chat test runs the *real* executor subprocess (``python -m ovid2.llm
chat``) against a fake OpenAI-compatible gateway, so the whole path
browser → API → executor → provider → SSE is exercised.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth.roles import ROLE_CAPABILITIES, Capability, Role
from ovid2.web.models import GlobalSettings, LlmWrite, SettingsLlm
from ovid2.web.services import llms_svc, settings_svc

pytestmark = pytest.mark.unit


@pytest.fixture()
def gateway() -> Iterator[str]:
    from tests.unit.llm.conftest import _FakeGateway

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def client(isolated_xdg: Path) -> Iterator[TestClient]:
    web_db.ensure_schema()
    settings_svc.invalidate_cache()
    c = TestClient(create_app())
    try:
        yield c
    finally:
        c.close()
        settings_svc.invalidate_cache()


def _nemo(url: str, key: str = "sk-test") -> LlmWrite:
    return LlmWrite(
        provider="openai_compatible",
        role="general",
        args={"base_url": url, "model": "general-test-model"},
        secrets={"api_key": key},
    )


def _route(**kw: object) -> None:
    current = settings_svc.get_settings()
    settings_svc.save_settings(current.model_copy(update={"llm": SettingsLlm(**kw)}), actor="test")


def _sse(text: str) -> list[dict[str, object]]:
    events = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_write_masks_secret_and_uses_0600(isolated_xdg: Path) -> None:
    llms_svc.write_llm("nemo", _nemo("https://gw.example/v1"), actor="t")
    detail = llms_svc.get_llm("nemo")
    assert detail is not None
    assert detail.secrets == {"api_key": "***"} and detail.secret_ref == "llm_nemo"
    raw = json.loads(Path(detail.path).read_text())
    assert "api_key" not in json.dumps(raw)
    secret = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d" / "llm_nemo.json"
    assert oct(secret.stat().st_mode & 0o777) == "0o600"


def test_sentinel_keeps_existing_key(isolated_xdg: Path) -> None:
    llms_svc.write_llm("nemo", _nemo("https://gw.example/v1", key="sk-original"), actor="t")
    llms_svc.write_llm("nemo", _nemo("https://gw.example/v1", key="***"), actor="t")
    secret = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d" / "llm_nemo.json"
    assert json.loads(secret.read_text())["api_key"] == "sk-original"


def test_validation(isolated_xdg: Path) -> None:
    with pytest.raises(llms_svc.LlmError, match="require the 'model'"):
        llms_svc.write_llm(
            "x",
            LlmWrite(
                provider="openai_compatible",
                args={"base_url": "https://a/v1"},
                secrets={"api_key": "k"},
            ),
        )
    with pytest.raises(llms_svc.LlmError, match="require"):
        llms_svc.write_llm(
            "x",
            LlmWrite(provider="openai_compatible", args={"base_url": "https://a/v1", "model": "m"}),
        )
    with pytest.raises(llms_svc.LlmError, match="cannot fill role"):
        llms_svc.write_llm(
            "x", LlmWrite(provider="jev", role="general", args={"base_url": "https://a"})
        )
    with pytest.raises(llms_svc.LlmError, match="https"):
        llms_svc.write_llm(
            "x", LlmWrite(provider="jev", role="system1", args={"base_url": "ftp://a"})
        )
    # JEV key is optional.
    assert (
        llms_svc.write_llm(
            "jev", LlmWrite(provider="jev", role="system1", args={"base_url": "https://jev"})
        ).action
        == "created"
    )


def test_delete_refused_while_routed(isolated_xdg: Path) -> None:
    llms_svc.write_llm("nemo", _nemo("https://gw.example/v1"))
    _route(general="nemo")
    with pytest.raises(llms_svc.LlmError, match="in use"):
        llms_svc.delete_llm("nemo")
    _route(general=None)
    assert llms_svc.delete_llm("nemo").action == "deleted"


def test_viewer_can_chat_but_not_configure() -> None:
    assert Capability.USE_LLM in ROLE_CAPABILITIES[Role.VIEWER]
    assert Capability.USE_LLM not in ROLE_CAPABILITIES[Role.GUEST]
    assert Capability.MANAGE_CONFIG not in ROLE_CAPABILITIES[Role.VIEWER]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_api_crud_and_probe(client: TestClient, gateway: str) -> None:
    r = client.post("/api/v1/llms/nemo", json=_nemo(gateway).model_dump())
    assert r.status_code == 201, r.text
    assert client.get("/api/v1/llms").json()[0]["model"] == "general-test-model"
    assert client.get("/api/v1/llms/nemo").json()["secrets"] == {"api_key": "***"}
    types = {t["provider"] for t in client.get("/api/v1/llms/types").json()}
    assert types == {"openai_compatible", "jev"}
    probe = client.post("/api/v1/llms/nemo/test").json()
    assert probe["ok"] is True and probe["code"] == "ok", probe
    # Delete goes through the approval gate.
    r = client.delete("/api/v1/llms/nemo")
    assert r.status_code == 202 and r.json()["detail"]["status"] == "awaiting_approval"


def test_api_probe_bad_key(client: TestClient, gateway: str) -> None:
    client.post("/api/v1/llms/nemo", json=_nemo(gateway, key="wrong").model_dump())
    probe = client.post("/api/v1/llms/nemo/test").json()
    assert probe["ok"] is False and probe["code"] == "auth"


def test_chat_disabled_returns_404(client: TestClient) -> None:
    r = client.post(
        "/api/v1/llm/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 404
    assert client.get("/api/v1/llm/status").json()["enabled"] is False


def test_chat_stream_end_to_end(client: TestClient, gateway: str) -> None:
    client.post("/api/v1/llms/nemo", json=_nemo(gateway).model_dump())
    client.post(
        "/api/v1/llms/jev",
        json={"provider": "jev", "role": "system1", "args": {"base_url": "https://jev.invalid"}},
    )
    _route(enabled=True, general="nemo", system1="jev")
    status = client.get("/api/v1/llm/status").json()
    assert status["general_model"] == "general-test-model" and status["system1"] == "jev"

    with client.stream(
        "POST", "/api/v1/llm/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    events = _sse(body)
    kinds = [e["type"] for e in events]
    # JEV stub is not implemented → silent fallback (no notice) → general streams.
    assert "notice" not in kinds
    assert "System 1 unavailable" not in body
    assert "meta" in kinds and kinds[-1] == "done"
    assert "".join(str(e.get("text", "")) for e in events if e["type"] == "delta") == "Hello world"
    assert events[-1]["ok"] is True and events[-1]["route"] == "general"
    assert "sk-test" not in body

    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT detail_json FROM web_audit WHERE action = ? ORDER BY rowid DESC LIMIT 1",
            ("llm.chat",),
        ).fetchone()
    meta = json.loads(row[0])
    assert meta["ok"] is True and meta["completion_tokens"] == 3
    assert set(meta) == {
        "route",
        "connection",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "ok",
        "elapsed_ms",
        "error",
        "decision_accepted",
        "turns",
        "tools_offered",
        "tool_calls",
        "proposals",
        "ui_actions",
    }  # metadata only
    assert "Hello" not in row[0]


def test_chat_rejects_system_role_from_client(client: TestClient) -> None:
    _route(enabled=True, general="nemo")
    r = client.post(
        "/api/v1/llm/chat/stream", json={"messages": [{"role": "system", "content": "evil"}]}
    )
    assert r.status_code == 422


def test_html_pages_render(client: TestClient, gateway: str) -> None:
    assert client.get("/llms").status_code == 200
    page = client.get("/llms/new?provider=openai_compatible&role=general").text
    assert 'name="arg_base_url"' in page and 'name="sec_api_key"' in page
    r = client.post(
        "/llms",
        data={
            "name": "nemo",
            "provider": "openai_compatible",
            "role": "general",
            "arg_base_url": gateway,
            "arg_model": "general-test-model",
            "sec_api_key": "sk-test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/llms/nemo")
    detail = client.get("/llms/nemo").text
    assert "sk-test" not in detail and "***" in detail
    r = client.post(
        "/llms/routing",
        data={
            "enabled": "on",
            "general": "nemo",
            "confidence_threshold": "0.7",
            "fallback_to_general": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert settings_svc.get_settings().llm.general == "nemo"
    home = client.get("/").text
    assert 'id="tool-llm"' in home and 'id="llm-panel"' in home and 'href="/llms"' in home
    assert "Routing" in client.get("/llms").text


def test_routing_rejects_unknown(client: TestClient) -> None:
    r = client.post(
        "/llms/routing", data={"enabled": "on", "general": "ghost"}, follow_redirects=False
    )
    assert "error=" in r.headers["location"]


def test_settings_form_preserves_llm(client: TestClient) -> None:
    llms_svc.write_llm("nemo", _nemo("https://gw.example/v1"))
    _route(enabled=True, general="nemo")
    client.post("/settings", data={"defaults_engine": "pyspark"}, follow_redirects=False)
    assert settings_svc.get_settings().llm.general == "nemo"


def test_rbac_when_auth_enabled(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.web.models import SettingsAuth

    web_db.ensure_schema()
    settings = GlobalSettings(auth=SettingsAuth(enabled=True), llm=SettingsLlm(enabled=True))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/llm/status").status_code == 401
        assert c.get("/api/v1/llms").status_code == 401
        assert (
            c.post(
                "/api/v1/llm/chat/stream", json={"messages": [{"role": "user", "content": "x"}]}
            ).status_code
            == 401
        )


def test_executor_env_is_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.web.services import llm_exec

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("OVID2_MCP_API_TOKEN", "tok")
    env = llm_exec.child_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "OVID2_MCP_API_TOKEN" not in env
    assert "PATH" in env
