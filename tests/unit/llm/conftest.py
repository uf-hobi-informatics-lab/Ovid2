"""Shared fixtures for the LLM runtime tests: a fake OpenAI-compatible server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class _FakeGateway(BaseHTTPRequestHandler):
    """Minimal ``/v1/models`` + ``/v1/chat/completions`` implementation."""

    api_key = "sk-test"
    models = ("general-test-model", "other")
    decision_reply = (
        '{"in_scope": true, "decision": "approve", "confidence": 0.9, '
        '"action": "noop", "rationale": "clear", "answer": "Approved."}'
    )
    requests: list[dict[str, object]] = []

    def log_message(self, *_args: object) -> None:  # silence
        return

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.api_key}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "Authentication Error - bad key"}}')
            return False
        return True

    def do_GET(self) -> None:
        if not self._authed():
            return
        body = json.dumps({"data": [{"id": m} for m in self.models]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(payload)
        if not payload.get("stream"):
            body = json.dumps({"choices": [{"message": {"content": self.decision_reply}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture()
def fake_gateway() -> Iterator[str]:
    """Start the fake gateway; yield its ``/v1`` base URL."""
    _FakeGateway.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def gateway_requests() -> list[dict[str, object]]:
    """Return the list of request bodies the fake gateway received."""
    return _FakeGateway.requests


@pytest.fixture()
def llm_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate XDG config so LLM JSON + secrets land in ``tmp_path``."""
    cfg = tmp_path / "config"
    (cfg / "ovid2" / "secrets.d").mkdir(parents=True)
    (cfg / "ovid2" / "secrets.d").chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.delenv("OVID2_LLMS_DIR", raising=False)
    monkeypatch.delenv("OVID2_SECRETS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    return cfg


def write_llm(
    cfg: Path, name: str, doc: dict[str, object], secret: dict[str, object] | None = None
) -> None:
    """Drop an LLM JSON (+ optional 0o600 secret) into the isolated config."""
    d = cfg / "ovid2" / "llms"
    d.mkdir(parents=True, exist_ok=True)
    if secret is not None:
        ref = f"llm_{name}"
        doc = {**doc, "secret_ref": ref}
        p = cfg / "ovid2" / "secrets.d" / f"{ref}.json"
        p.write_text(json.dumps(secret))
        p.chmod(0o600)
    (d / f"{name}.json").write_text(json.dumps(doc))
