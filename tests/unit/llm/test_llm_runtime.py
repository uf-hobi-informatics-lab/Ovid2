"""Unit tests for the executor-tier LLM runtime (providers, router, CLI)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from ovid2.config import llms as llms_config
from ovid2.llm import cli as llm_cli
from ovid2.llm.protocol import Decision, Provider, ProviderError
from ovid2.llm.providers import PROVIDER_TYPE_MAP, load_provider_class
from ovid2.llm.providers.openai_compatible import (
    OpenAiCompatibleProvider,
    parse_decision,
    trim_to_context,
)
from ovid2.llm.router import RouteConfig, route_chat, sanitize_messages
from tests.unit.llm.conftest import write_llm

pytestmark = pytest.mark.unit

USER = [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_provider_catalogue_matches_registry() -> None:
    assert set(PROVIDER_TYPE_MAP) == set(llms_config.list_providers())
    for name in PROVIDER_TYPE_MAP:
        cls = load_provider_class(name)
        assert cls.provider_name == name
        assert set(cls.roles) == set(llms_config.provider_roles(name))


def test_validate_rejects_inline_secret_and_bad_role() -> None:
    with pytest.raises(ValueError, match="inline"):
        llms_config.validate_llm_doc(
            "x", {"provider": "openai_compatible", "args": {"api_key": "sk"}}
        )
    with pytest.raises(ValueError, match="cannot fill role"):
        llms_config.validate_llm_doc("x", {"provider": "jev", "role": "general"})
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llms_config.validate_llm_doc("x", {"provider": "nope"})


# ---------------------------------------------------------------------------
# OpenAI-compatible provider against the fake gateway
# ---------------------------------------------------------------------------


def _provider(url: str, key: str = "sk-test", model: str = "general-test-model") -> Provider:
    return OpenAiCompatibleProvider("n", {"base_url": url, "model": model}, {"api_key": key})


def test_probe_ok(fake_gateway: str) -> None:
    result = _provider(fake_gateway).probe()
    assert result.ok and result.code == "ok"


def test_probe_bad_key(fake_gateway: str) -> None:
    result = _provider(fake_gateway, key="wrong").probe()
    assert not result.ok and result.code == "auth"
    assert "Authentication Error" in result.message


def test_probe_model_not_found(fake_gateway: str) -> None:
    result = _provider(fake_gateway, model="missing").probe()
    assert not result.ok and result.code == "model_not_found"


def test_probe_unreachable() -> None:
    result = _provider("http://127.0.0.1:9/v1").probe()
    assert not result.ok and result.code in {"network", "timeout"}


def test_chat_stream_parses_reasoning_delta_usage(
    fake_gateway: str, gateway_requests: list[dict[str, object]]
) -> None:
    events = list(_provider(fake_gateway).chat_stream(USER))
    kinds = [e["type"] for e in events]
    assert kinds == ["reasoning", "delta", "delta", "usage"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello world"
    assert events[-1]["completion_tokens"] == 3
    body = gateway_requests[-1]
    assert body["model"] == "general-test-model" and body["stream"] is True


def test_chat_stream_auth_error(fake_gateway: str) -> None:
    with pytest.raises(ProviderError) as info:
        list(_provider(fake_gateway, key="bad").chat_stream(USER))
    assert info.value.code == "auth"


def test_decide_via_openai_compatible(fake_gateway: str) -> None:
    decision = _provider(fake_gateway).decide(USER)
    assert decision.in_scope and decision.decision == "approve"
    assert decision.confidence == pytest.approx(0.9)


def test_parse_decision_tolerates_fences_and_garbage() -> None:
    d = parse_decision('```json\n{"in_scope": true, "decision": "x", "confidence": 3}\n```')
    assert d.confidence == 1.0 and d.decision == "x"
    bad = parse_decision("no json here")
    assert not bad.in_scope and bad.confidence == 0.0


def test_trim_to_context_keeps_system_and_last_turn() -> None:
    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "x" * 400} for _ in range(10)
    ]
    out = trim_to_context(msgs, context_window=300, reserve=100)
    assert out[0]["role"] == "system"
    assert len(out) < len(msgs) and len(out) >= 2


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class _S1(Provider):
    provider_name = "fake_s1"
    roles = ("system1",)

    def __init__(self, decision: Decision | Exception) -> None:
        super().__init__("s1", {"model": "jev-x"}, {})
        self._d = decision

    def probe(self):  # type: ignore[no-untyped-def]
        raise AssertionError

    def decide(self, messages):  # type: ignore[no-untyped-def]
        if isinstance(self._d, Exception):
            raise self._d
        return self._d


class _General(Provider):
    provider_name = "fake_general"
    roles = ("general",)

    def __init__(self) -> None:
        super().__init__("g", {"model": "nemo"}, {})
        self.seen: list[list[dict[str, str]]] = []

    def probe(self):  # type: ignore[no-untyped-def]
        raise AssertionError

    def chat_stream(self, messages):  # type: ignore[no-untyped-def]
        self.seen.append(messages)
        yield {"type": "delta", "text": "general answer"}


def _run(route: RouteConfig, s1: Provider | None, general: Provider | None) -> list[dict]:
    def factory(name: str) -> Provider:
        if name == "s1" and s1 is not None:
            return s1
        if name == "g" and general is not None:
            return general
        raise FileNotFoundError(name)

    return list(route_chat(USER, route, factory))


def test_router_general_only() -> None:
    g = _General()
    events = _run(RouteConfig(general="g"), None, g)
    assert [e["type"] for e in events] == ["meta", "delta", "done"]
    assert events[0]["route"] == "general" and events[-1]["ok"]
    assert g.seen[0][0]["role"] == "system"  # base prompt injected


def test_router_system1_accepts() -> None:
    s1 = _S1(Decision(True, "approve", 0.95, answer="Yes."))
    events = _run(RouteConfig(general="g", system1="s1"), s1, _General())
    kinds = [e["type"] for e in events]
    assert kinds == ["decision", "meta", "delta", "done"]
    assert events[0]["accepted"] is True and events[-1]["route"] == "system1"


def test_router_system1_low_confidence_falls_back() -> None:
    s1 = _S1(Decision(True, "maybe", 0.3))
    events = _run(RouteConfig(general="g", system1="s1"), s1, _General())
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["decision", "notice"]
    assert events[0]["accepted"] is False
    assert events[-1] == {**events[-1], "route": "general", "ok": True}


def test_router_jev_not_implemented_falls_back() -> None:
    s1 = _S1(NotImplementedError("JEV provider is not available yet."))
    events = _run(RouteConfig(general="g", system1="s1"), s1, _General())
    # Stub System 1 → silent fallback: no user-visible notice.
    assert all(e["type"] != "notice" for e in events)
    assert events[-1]["route"] == "general" and events[-1]["ok"]


def test_router_no_fallback_errors() -> None:
    s1 = _S1(Decision(False, "", 0.0))
    events = _run(RouteConfig(general="g", system1="s1", fallback_to_general=False), s1, _General())
    assert any(e["type"] == "error" and e["code"] == "no_route" for e in events)
    assert events[-1]["ok"] is False


def test_router_no_general_configured() -> None:
    events = _run(RouteConfig(), None, None)
    assert events[0]["code"] == "no_route" and events[-1]["type"] == "done"


def test_router_requires_trailing_user_message() -> None:
    events = list(
        route_chat(
            [{"role": "assistant", "content": "x"}], RouteConfig(general="g"), lambda n: _General()
        )
    )
    assert events[0]["code"] == "bad_request"


def test_sanitize_messages_drops_system_and_junk() -> None:
    raw = [
        {"role": "system", "content": "evil"},
        {"role": "user", "content": "ok"},
        {"role": "user", "content": 5},
        "x",
        {"role": "assistant", "content": " "},
    ]
    assert sanitize_messages(raw) == [{"role": "user", "content": "ok"}]


# ---------------------------------------------------------------------------
# Executor CLI end-to-end (real config + secret file, fake gateway)
# ---------------------------------------------------------------------------


def test_cli_probe_and_chat(llm_xdg: Path, fake_gateway: str) -> None:
    write_llm(
        llm_xdg,
        "nemo",
        {
            "provider": "openai_compatible",
            "role": "general",
            "args": {"base_url": fake_gateway, "model": "general-test-model"},
        },
        secret={"api_key": "sk-test"},
    )
    out = io.StringIO()
    assert llm_cli.probe("nemo", out=out) == 0
    assert json.loads(out.getvalue())["ok"] is True

    request = {"messages": USER, "route": {"general": "nemo", "system1": None}}
    out = io.StringIO()
    rc = llm_cli.chat(stdin=io.StringIO(json.dumps(request)), out=out)
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert rc == 0
    assert events[0] == {
        "type": "meta",
        "route": "general",
        "connection": "nemo",
        "model": "general-test-model",
    }
    assert events[-1]["type"] == "done" and events[-1]["ok"] is True
    assert "sk-test" not in out.getvalue()


def test_cli_chat_missing_connection(llm_xdg: Path) -> None:
    out = io.StringIO()
    rc = llm_cli.chat(
        stdin=io.StringIO(json.dumps({"messages": USER, "route": {"general": "ghost"}})), out=out
    )
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert rc == 1 and events[0]["code"] == "config" and events[-1]["type"] == "done"


def test_cli_chat_bad_json(llm_xdg: Path) -> None:
    out = io.StringIO()
    assert llm_cli.chat(stdin=io.StringIO("{not json"), out=out) == 1
    assert json.loads(out.getvalue().splitlines()[0])["code"] == "bad_request"


def test_cli_probe_jev_not_implemented(llm_xdg: Path) -> None:
    write_llm(
        llm_xdg,
        "jev",
        {"provider": "jev", "role": "system1", "args": {"base_url": "https://jev.invalid"}},
    )
    out = io.StringIO()
    assert llm_cli.probe("jev", out=out) == 1
    assert json.loads(out.getvalue())["code"] == "not_implemented"
