"""Web-tier tests for assistant tools (L2).

- The per-turn assistant token is GET-only, role-capped and expiring,
  and is honoured even while auth is disabled (no synthetic-admin bypass).
- ``GET /api/v1/llm/tools`` reflects ``settings.llm.tools_enabled`` + RBAC.
- End to end: a real uvicorn server on a socket, the real ``python -m
  ovid2.llm chat`` executor, and a fake gateway that asks for a tool call.
  The executor calls back into the API with its token, the API answers,
  the model's write tool becomes a proposal, and the audit row records
  only metadata attributed to ``llm:<user>``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Role
from ovid2.web.models import LlmWrite, SettingsLlm
from ovid2.web.services import llm_chat_svc, settings_svc

pytestmark = pytest.mark.unit


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


def _route(**kw: Any) -> None:
    current = settings_svc.get_settings()
    settings_svc.save_settings(current.model_copy(update={"llm": SettingsLlm(**kw)}),
                               actor="test")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Token semantics
# ---------------------------------------------------------------------------


def test_cap_role() -> None:
    assert llm_token.cap_role(Role.SYSTEM_ADMIN) == Role.PIPELINE_OPERATOR
    assert llm_token.cap_role(Role.VIEWER) == Role.VIEWER


def test_token_get_allowed_and_attributed(client: TestClient) -> None:
    tok = llm_token.mint("alice", Role.SYSTEM_ADMIN, email="a@x", ttl_seconds=30)
    try:
        assert client.get("/api/v1/jobs", headers=_bearer(tok)).status_code == 200
    finally:
        llm_token.revoke(tok)


def test_token_cannot_write_even_with_auth_disabled(client: TestClient) -> None:
    tok = llm_token.mint("alice", Role.SYSTEM_ADMIN, email=None, ttl_seconds=30)
    try:
        r = client.post("/api/v1/runs", headers=_bearer(tok),
                        json={"pipeline": "p", "batch_id": "1"})
        assert r.status_code == 403
        assert client.delete("/api/v1/secrets/x", headers=_bearer(tok)).status_code == 403
        assert client.put("/api/v1/settings", headers=_bearer(tok), json={}).status_code == 403
    finally:
        llm_token.revoke(tok)


def test_token_role_is_capped(client: TestClient) -> None:
    """Admin-only GETs are refused: the token carries pipeline_operator at most."""
    tok = llm_token.mint("root", Role.SYSTEM_ADMIN, email=None, ttl_seconds=30)
    try:
        assert client.get("/api/v1/secrets", headers=_bearer(tok)).status_code == 403
        assert client.get("/api/v1/install/status", headers=_bearer(tok)).status_code == 403
        assert client.get("/api/v1/llms", headers=_bearer(tok)).status_code == 403
    finally:
        llm_token.revoke(tok)


def test_token_cannot_start_nested_chat(client: TestClient) -> None:
    _route(enabled=True, general="nemo", tools_enabled=True)
    tok = llm_token.mint("alice", Role.VIEWER, email=None, ttl_seconds=30)
    try:
        r = client.post("/api/v1/llm/chat/stream", headers=_bearer(tok),
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403
    finally:
        llm_token.revoke(tok)


def test_revoked_and_expired_tokens_rejected(client: TestClient) -> None:
    tok = llm_token.mint("alice", Role.VIEWER, email=None, ttl_seconds=30)
    llm_token.revoke(tok)
    assert client.get("/api/v1/jobs", headers=_bearer(tok)).status_code == 401
    short = llm_token.mint("alice", Role.VIEWER, email=None, ttl_seconds=1)
    time.sleep(1.1)
    assert client.get("/api/v1/jobs", headers=_bearer(short)).status_code == 401


def test_unrelated_bearer_falls_through(client: TestClient) -> None:
    """A non-assistant bearer is not treated as an assistant token."""
    r = client.get("/api/v1/jobs", headers=_bearer("some-other-token"))
    assert r.status_code == 200  # auth disabled → synthetic admin


# ---------------------------------------------------------------------------
# Tool listing + request building
# ---------------------------------------------------------------------------


def test_llm_tools_endpoint(client: TestClient) -> None:
    _route(enabled=True, general="nemo", tools_enabled=False)
    assert client.get("/api/v1/llm/tools").json() == []
    _route(enabled=True, general="nemo", tools_enabled=True)
    tools = client.get("/api/v1/llm/tools").json()
    names = {t["name"] for t in tools}
    assert {"list_jobs", "get_job_log", "submit_run", "cancel_job"} <= names
    assert all(t["mutating"] == (t["method"] not in ("GET", "")) for t in tools)
    ui = next(t for t in tools if t["name"] == "set_appearance")
    assert ui["kind"] == "ui_action" and ui["ui_action"] is True and ui["path"] == ""


def test_allowed_tools_respects_role() -> None:
    viewer = {t.name for t in llm_chat_svc.allowed_tools(Role.VIEWER)}
    assert "list_jobs" in viewer and "list_connections" not in viewer
    assert "submit_run" not in viewer  # viewer cannot submit, so no proposal either
    guest = {t.name for t in llm_chat_svc.allowed_tools(Role.GUEST)}
    assert guest == {"health", "list_cdms", "get_cdm"}


def test_build_request_has_no_tools_when_disabled(client: TestClient) -> None:
    from ovid2.web.models import LlmChatRequest

    body = LlmChatRequest(messages=[{"role": "user", "content": "x"}])
    p = llm_chat_svc.ChatPrincipal("u", Role.SYSTEM_ADMIN, api_url="http://h:1")
    _route(enabled=True, general="nemo", tools_enabled=False)
    assert llm_chat_svc.build_request(body, p)["route"]["tools"] == []
    _route(enabled=True, general="nemo", tools_enabled=True)
    assert "list_jobs" in llm_chat_svc.build_request(body, p)["route"]["tools"]


# ---------------------------------------------------------------------------
# End to end over real sockets + the real executor subprocess
# ---------------------------------------------------------------------------


class _ToolGateway(BaseHTTPRequestHandler):
    """Fake OpenAI gateway: 1st turn → list_jobs, 2nd → cancel_job, else text."""

    requests: list[dict[str, Any]] = []

    def log_message(self, *_a: object) -> None:
        return

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(payload)
        tool_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if not tool_msgs and payload.get("tools"):
            chunks = [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "function": {"name": "list_jobs",
                                                          "arguments": '{"limit": 5}'}}]}}]}]
        elif len(tool_msgs) == 1 and payload.get("tools"):
            chunks = [{"choices": [{"delta": {"content": "Cancelling."}}]},
                      {"choices": [{"delta": {"tool_calls": [
                          {"index": 0, "id": "call_b", "function": {
                              "name": "cancel_job", "arguments": '{"job_id": "j-42"}'}}]}}]}]
        else:
            chunks = [{"choices": [{"delta": {"content": "done"}}]}]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def tool_gateway() -> Iterator[str]:
    _ToolGateway.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ToolGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def live_server(isolated_xdg: Path) -> Iterator[str]:
    import uvicorn

    web_db.ensure_schema()
    settings_svc.invalidate_cache()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="warning", ws="none",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        settings_svc.invalidate_cache()


def test_chat_with_tools_end_to_end(live_server: str, tool_gateway: str) -> None:
    from ovid2.web.services import llms_svc

    llms_svc.write_llm("nemo", LlmWrite(provider="openai_compatible", role="general",
                                        args={"base_url": tool_gateway, "model": "m"},
                                        secrets={"api_key": "sk-test"}))
    _route(enabled=True, general="nemo", tools_enabled=True)
    with httpx.stream("POST", f"{live_server}/api/v1/llm/chat/stream", timeout=60,
                      json={"messages": [{"role": "user", "content": "cancel my job"}]}) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    events = [json.loads(line[5:].strip()) for frame in body.split("\n\n")
              for line in frame.splitlines() if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done" and events[-1]["ok"] is True, events
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "list_jobs" and call["path"] == "/api/v1/jobs"
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["status"] == 200 and result["ok"] is True  # executor hit the real API
    proposal = next(e for e in events if e["type"] == "proposal")
    assert proposal["method"] == "POST" and proposal["path"] == "/api/v1/jobs/j-42/cancel"
    assert "_tool_calls" not in kinds

    # The model saw tools, then the API result as an untrusted tool message.
    first, second = _ToolGateway.requests[0], _ToolGateway.requests[1]
    assert {t["function"]["name"] for t in first["tools"]} >= {"list_jobs", "cancel_job"}
    tool_msg = json.loads(next(m for m in second["messages"] if m["role"] == "tool")["content"])
    assert tool_msg["http_status"] == 200 and "jobs" in tool_msg["data"]
    assert not any("ovllm_" in json.dumps(req) for req in _ToolGateway.requests)

    # Token revoked after the turn; audit is metadata-only.
    with web_db.connect() as conn:
        rows = conn.execute("SELECT actor, action, detail_json FROM web_audit "
                            "ORDER BY rowid").fetchall()
    chat = [r for r in rows if r[1] == "llm.chat"][-1]
    meta = json.loads(chat[2])
    assert meta["tool_calls"] == [{"name": "list_jobs", "status": 200,
                                   "ms": result["elapsed_ms"]}]
    assert meta["proposals"] == [{"name": "cancel_job", "method": "POST",
                                  "path": "/api/v1/jobs/j-42/cancel"}]
    assert "Cancelling" not in chat[2]
    assert not llm_token._GRANTS


# ---------------------------------------------------------------------------
# UI actions (set_appearance) — browser-only, audited by name only
# ---------------------------------------------------------------------------


def test_ui_actions_offered_to_llm_users_only() -> None:
    viewer = {t.name for t in llm_chat_svc.allowed_tools(Role.VIEWER)}
    assert {"set_appearance", "save_appearance", "apply_saved_appearance"} <= viewer
    guest = {t.name for t in llm_chat_svc.allowed_tools(Role.GUEST)}
    assert "set_appearance" not in guest  # guests cannot use the assistant at all


def test_build_request_forwards_ui_context_only_with_ui_tools(client: TestClient) -> None:
    from ovid2.web.models import LlmChatRequest

    body = LlmChatRequest(messages=[{"role": "user", "content": "x"}],
                          ui_context={"theme": "matrix", "saved_themes": ["night"]})
    p = llm_chat_svc.ChatPrincipal("u", Role.VIEWER, api_url="http://h:1")
    _route(enabled=True, general="nemo", tools_enabled=True)
    req = llm_chat_svc.build_request(body, p)
    assert req["ui_context"]["theme"] == "matrix"
    _route(enabled=True, general="nemo", tools_enabled=False)
    assert llm_chat_svc.build_request(body, p)["ui_context"] is None


def test_ui_context_is_bounded(client: TestClient) -> None:
    _route(enabled=True, general="nemo", tools_enabled=True)
    r = client.post("/api/v1/llm/chat/stream",
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "ui_context": {"theme": "Bad Theme!"}})
    assert r.status_code == 422


class _UiGateway(BaseHTTPRequestHandler):
    """Fake gateway: 1st turn → set_appearance, then a confirmation."""

    requests: list[dict[str, Any]] = []

    def log_message(self, *_a: object) -> None:
        return

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(payload)
        tool_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if not tool_msgs:
            args = json.dumps({"theme": "tatooine", "appearance": {"--body-fs": "16px"}})
            chunks = [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_ui", "function": {"name": "set_appearance",
                                                           "arguments": args}}]}}]}]
        else:
            chunks = [{"choices": [{"delta": {"content": "Switched to Tatooine."}}]}]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def test_set_appearance_end_to_end(live_server: str) -> None:
    from ovid2.web.services import llms_svc

    _UiGateway.requests = []
    gw = ThreadingHTTPServer(("127.0.0.1", 0), _UiGateway)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    try:
        llms_svc.write_llm("nemo", LlmWrite(
            provider="openai_compatible", role="general",
            args={"base_url": f"http://127.0.0.1:{gw.server_address[1]}/v1", "model": "m"},
            secrets={"api_key": "sk-test"}))
        _route(enabled=True, general="nemo", tools_enabled=True)
        with httpx.stream("POST", f"{live_server}/api/v1/llm/chat/stream", timeout=60,
                          json={"messages": [{"role": "user", "content": "make it sandy"}],
                                "ui_context": {"theme": "clinical",
                                               "saved_themes": ["night"]}}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
    finally:
        gw.shutdown()
        gw.server_close()
    events = [json.loads(line[5:].strip()) for frame in body.split("\n\n")
              for line in frame.splitlines() if line.startswith("data:")]
    ui = [e for e in events if e["type"] == "ui_action"]
    assert ui and ui[0]["name"] == "set_appearance"
    assert ui[0]["args"] == {"theme": "tatooine", "appearance": {"--body-fs": "16px"}}
    assert events[-1]["type"] == "done" and events[-1]["ok"] is True
    assert any(e["type"] == "delta" and "Tatooine" in e["text"] for e in events)
    # The model saw the current look in its system prompt.
    system = _UiGateway.requests[0]["messages"][0]["content"]
    assert "theme=clinical" in system and '["night"]' in system
    # Audit: the action name only, never its arguments.
    with web_db.connect() as conn:
        rows = conn.execute("SELECT action, detail_json FROM web_audit ORDER BY rowid").fetchall()
    detail = [r for r in rows if r[0] == "llm.chat"][-1][1]
    assert json.loads(detail)["ui_actions"] == [{"name": "set_appearance"}]
    assert "tatooine" not in detail and "16px" not in detail
