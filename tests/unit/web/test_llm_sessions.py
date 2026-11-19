"""Saved assistant sessions: store, search, REST surface, chat persistence, titles.

The chat tests run the real executor subprocess (``python -m ovid2.llm
chat`` / ``title``) against the fake OpenAI-compatible gateway.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Role
from ovid2.web.models import LlmWrite, SettingsLlm
from ovid2.web.services import llm_sessions_svc as svc
from ovid2.web.services import llms_svc, settings_svc

pytestmark = pytest.mark.unit


@pytest.fixture()
def gateway() -> Iterator[str]:
    from tests.unit.llm.conftest import _FakeGateway

    _FakeGateway.requests = []
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
    svc._FTS_READY.clear()
    settings_svc.invalidate_cache()
    c = TestClient(create_app())
    try:
        yield c
    finally:
        c.close()
        settings_svc.invalidate_cache()


def _route(general: str) -> None:
    current = settings_svc.get_settings()
    settings_svc.save_settings(
        current.model_copy(update={"llm": SettingsLlm(enabled=True, general=general)}),
        actor="test",
    )


def _connect(client: TestClient, gateway: str) -> None:
    client.post(
        "/api/v1/llms/nemo",
        json=LlmWrite(
            provider="openai_compatible",
            role="general",
            args={"base_url": gateway, "model": "general-test-model", "context_window": "32000"},
            secrets={"api_key": "sk-test"},
        ).model_dump(),
    )
    _route("nemo")


def _sse(body: str) -> list[dict[str, object]]:
    out = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


def _chat(client: TestClient, text: str, session_id: str | None = None) -> list[dict[str, object]]:
    body: dict[str, object] = {"messages": [{"role": "user", "content": text}]}
    if session_id:
        body["session_id"] = session_id
    with client.stream("POST", "/api/v1/llm/chat/stream", json=body) as resp:
        assert resp.status_code == 200, resp.read()
        return _sse("".join(resp.iter_text()))


def _wait_title(client: TestClient, sid: str) -> dict[str, object]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        row = client.get(f"/api/v1/llm/sessions/{sid}").json()
        if row["title_source"] != "pending":
            return row
        time.sleep(0.2)
    raise AssertionError("title never generated")


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


def test_service_crud_search_and_isolation(isolated_xdg: Path) -> None:
    svc._FTS_READY.clear()
    a = svc.create_session("alice")
    svc.append_turn(
        "alice",
        a.session_id,
        [
            {"role": "user", "content": "Copy the DEMOGRAPHIC table to snowflake"},
            {"role": "assistant", "content": "Use the copy run kind.", "meta": {"tools": []}},
        ],
        prompt_tokens=100,
        completion_tokens=20,
        context_tokens=120,
    )
    b = svc.create_session("alice", "Vocabulary questions")
    svc.append_turn("alice", b.session_id, [{"role": "user", "content": "what is LOINC"}])
    svc.create_session("bob")

    rows = svc.list_sessions("alice")
    assert [r.session_id for r in rows] == [b.session_id, a.session_id]
    assert rows[1].last_prompt_tokens == 120 and rows[1].total_prompt_tokens == 100
    assert rows[1].message_count == 2

    hits = svc.list_sessions("alice", query="snowflake")
    assert [h.session_id for h in hits] == [a.session_id]
    assert hits[0].snippet and "snowflake" in hits[0].snippet.lower()
    # prefix match, all terms required, title matched too
    assert [h.session_id for h in svc.list_sessions("alice", query="demo copy")] == [a.session_id]
    assert svc.list_sessions("alice", query="demo loinc") == []
    assert [h.session_id for h in svc.list_sessions("alice", query="vocabulary")] == [b.session_id]
    # FTS operators in user input are inert
    assert svc.list_sessions("alice", query='"NEAR( OR *') == []
    # another user sees nothing of alice's
    assert svc.list_sessions("bob", query="snowflake") == []
    with pytest.raises(svc.SessionNotFoundError):
        svc.get_session("bob", a.session_id)

    detail = svc.get_session("alice", a.session_id)
    assert [m["seq"] for m in detail.messages] == [1, 2]
    svc.update_message_meta(
        "alice", a.session_id, 2, {"tools": [], "proposals": [{"state": "done"}]}
    )
    assert (
        svc.get_session("alice", a.session_id).messages[1]["meta"]["proposals"][0]["state"]
        == "done"
    )

    assert svc.set_generated_title(a.session_id, '"Copy demographic"', "llm") is True
    svc.rename_session("alice", a.session_id, "Mine")
    assert svc.set_generated_title(a.session_id, "Late title", "llm") is False
    assert svc.get_session("alice", a.session_id).title == "Mine"

    svc.delete_session("alice", a.session_id)
    assert svc.list_sessions("alice", query="snowflake") == []
    with web_db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM web_llm_messages WHERE session_id = ?", (a.session_id,)
        ).fetchone()[0]
    assert n == 0


def test_like_fallback_when_fts_unavailable(isolated_xdg: Path) -> None:
    svc._FTS_READY.clear()
    s = svc.create_session("u")
    svc.append_turn("u", s.session_id, [{"role": "user", "content": "health check cycle"}])
    from ovid2.web.settings import web_db_path

    svc._FTS_READY[str(web_db_path())] = False
    try:
        assert [h.session_id for h in svc.list_sessions("u", query="cycle health")] == [
            s.session_id
        ]
        assert svc.list_sessions("u", query="cycle omop") == []
    finally:
        svc._FTS_READY.clear()


def test_fallback_title() -> None:
    assert svc.fallback_title("  hi   there ") == "hi there"
    long = svc.fallback_title("word " * 40)
    assert len(long) <= 61 and long.endswith("…")
    assert svc.fallback_title("") == "New session"


# --------------------------------------------------------------------------
# REST + chat
# --------------------------------------------------------------------------


def test_chat_saves_session_and_generates_title(client: TestClient, gateway: str) -> None:
    from tests.unit.llm.conftest import _FakeGateway

    _connect(client, gateway)
    assert client.get("/api/v1/llm/status").json()["context_window"] == 32000

    events = _chat(client, "How do I copy a table to snowflake?")
    sid = str(events[0]["session_id"])
    session_ev = events[-1]
    assert session_ev["type"] == "session" and session_ev["context_window"] == 32000
    assert session_ev["last_prompt_tokens"] == 8 and session_ev["assistant_seq"] == 2

    # The title comes from the general model (fake gateway replies "Hello world").
    row = _wait_title(client, sid)
    assert row["title_source"] == "llm" and row["title"] == "Hello world"
    assert [m["role"] for m in row["messages"]] == ["user", "assistant"]
    assert row["messages"][1]["content"] == "Hello world"
    assert row["messages"][1]["meta"]["model"] == "general-test-model"

    # Second turn: browser sends only the new message, server prepends history.
    _FakeGateway.requests = []
    _chat(client, "and then?", sid)
    sent = [r for r in _FakeGateway.requests if r.get("stream")][-1]["messages"]
    contents = [m["content"] for m in sent if m["role"] != "system"]
    assert contents[-3:] == ["How do I copy a table to snowflake?", "Hello world", "and then?"]
    detail = client.get(f"/api/v1/llm/sessions/{sid}").json()
    assert detail["message_count"] == 4 and detail["total_completion_tokens"] == 6
    assert detail["title"] == "Hello world"  # not re-titled

    # Search, rename, delete.
    hits = client.get("/api/v1/llm/sessions", params={"q": "snowflake"}).json()
    assert [h["session_id"] for h in hits] == [sid] and "snowflake" in hits[0]["snippet"]
    r = client.patch(f"/api/v1/llm/sessions/{sid}", json={"title": "Snowflake copy"})
    assert r.status_code == 200 and r.json()["title_source"] == "user"
    assert client.delete(f"/api/v1/llm/sessions/{sid}").status_code == 204
    assert client.get(f"/api/v1/llm/sessions/{sid}").status_code == 404

    # Audit rows stay metadata-only.
    with web_db.connect() as conn:
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT detail_json FROM web_audit WHERE action = 'llm.chat'"
            ).fetchall()
        ]
    assert rows and all("snowflake" not in r and "Hello" not in r for r in rows)


def test_title_falls_back_when_model_fails(client: TestClient, gateway: str) -> None:
    _connect(client, gateway)
    from ovid2.web.services import llm_exec

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise llm_exec.ExecError("down")

    orig = llm_exec.run_title
    llm_exec.run_title = boom  # type: ignore[assignment]
    try:
        sid = str(_chat(client, "Explain the overwrite gate please")[0]["session_id"])
        row = _wait_title(client, sid)
    finally:
        llm_exec.run_title = orig  # type: ignore[assignment]
    assert row["title_source"] == "fallback" and row["title"] == "Explain the overwrite gate please"


def test_sessions_api_isolation_and_machine_principals(client: TestClient) -> None:
    created = client.post("/api/v1/llm/sessions", json={"title": "Mine"})
    assert created.status_code == 201
    sid = created.json()["session_id"]
    assert client.get("/api/v1/llm/sessions").json()[0]["title"] == "Mine"
    assert client.get("/api/v1/llm/sessions/not-an-id").status_code == 404

    # The assistant's own per-turn token may not read a human's sessions.
    tok = llm_token.mint("anonymous", Role.SYSTEM_ADMIN, email=None, ttl_seconds=30)
    try:
        hdr = {"Authorization": f"Bearer {tok}"}
        assert client.get(f"/api/v1/llm/sessions/{sid}", headers=hdr).status_code == 403
        assert client.get("/api/v1/llm/sessions", headers=hdr).status_code == 403
    finally:
        llm_token.revoke(tok)
    # Another principal cannot see or delete it.
    with pytest.raises(svc.SessionNotFoundError):
        svc.get_session("someone-else", sid)
    with pytest.raises(svc.SessionNotFoundError):
        svc.delete_session("someone-else", sid)
    assert client.get(f"/api/v1/llm/sessions/{sid}").status_code == 200


def test_chat_with_foreign_session_is_404(client: TestClient, gateway: str) -> None:
    _connect(client, gateway)
    other = svc.create_session("someone-else")
    r = client.post(
        "/api/v1/llm/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "session_id": other.session_id},
    )
    assert r.status_code == 404


def test_executor_title_subcommand(llm_xdg_gateway: tuple[Path, str]) -> None:
    from ovid2.llm import cli

    out = io.StringIO()
    rc = cli.title(io.StringIO(json.dumps({"connection": "nemo", "prompt": "hi"})), out)
    result = json.loads(out.getvalue().strip())
    assert rc == 0 and result["ok"] is True and result["title"] == "Hello world"
    assert result["completion_tokens"] == 3

    out = io.StringIO()
    assert cli.title(io.StringIO("{}"), out) == 1
    assert json.loads(out.getvalue())["code"] == "bad_request"


@pytest.fixture()
def llm_xdg_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: str
) -> tuple[Path, str]:
    from tests.unit.llm.conftest import write_llm

    cfg = tmp_path / "config"
    (cfg / "ovid2" / "secrets.d").mkdir(parents=True)
    (cfg / "ovid2" / "secrets.d").chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.delenv("OVID2_LLMS_DIR", raising=False)
    monkeypatch.delenv("OVID2_SECRETS_FILE", raising=False)
    monkeypatch.delenv("OVID2_SECRETS_DIR", raising=False)
    write_llm(
        cfg,
        "nemo",
        {
            "provider": "openai_compatible",
            "role": "general",
            "args": {"base_url": gateway, "model": "general-test-model"},
        },
        {"api_key": "sk-test"},
    )
    return cfg, gateway


def test_context_window_default(client: TestClient, gateway: str) -> None:
    client.post(
        "/api/v1/llms/plain",
        json=LlmWrite(
            provider="openai_compatible",
            role="general",
            args={"base_url": gateway, "model": "m"},
            secrets={"api_key": "sk-test"},
        ).model_dump(),
    )
    assert llms_svc.context_window("plain") == 128000
    assert llms_svc.context_window(None) is None
