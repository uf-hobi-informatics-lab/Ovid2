"""OpenAI-compatible chat-completions provider (general LLM, or System 1 in a pinch).

Speaks ``POST {base_url}/chat/completions`` with ``stream: true`` and
parses the ``data: {...}`` SSE frames. Handles the ``reasoning_content``
delta field some gateways (e.g. vLLM-served reasoning models) emit for
"thinking" tokens and surfaces it as ``reasoning`` events so the UI can
render it separately from the answer.

The same class can fill the **system1** role: :meth:`decide` asks the
model for a strict JSON decision object. That is a stop-gap until JEV
ships a native decision endpoint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from ovid2.llm.protocol import (
    ChatMessage,
    Decision,
    LlmEvent,
    ProbeResult,
    Provider,
    ProviderError,
)

_DECISION_INSTRUCTIONS = (
    "You are System 1, a fast decision model. Reply with ONLY a JSON object "
    'of the form {"in_scope": bool, "decision": str, "confidence": number 0-1, '
    '"action": str|null, "rationale": str, "answer": str}. Set in_scope=false '
    "when the request is an open-ended question you should not decide."
)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _msg_chars(m: ChatMessage) -> int:
    n = len(str(m.get("content") or ""))
    for call in m.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            n += len(str(fn.get("arguments") or "")) + len(str(fn.get("name") or ""))
    return n


def trim_to_context(
    messages: list[ChatMessage], context_window: int, reserve: int
) -> list[ChatMessage]:
    """Drop the oldest non-system turns until the rough token estimate fits.

    Uses a 4-chars-per-token heuristic; good enough to keep requests
    under the gateway limit without a tokenizer dependency. Never leaves
    an orphan ``tool`` message at the head (a ``tool`` reply without its
    preceding assistant ``tool_calls`` is rejected by OpenAI-style APIs).
    """
    budget_chars = max(1, context_window - reserve) * 4
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    def size(ms: list[ChatMessage]) -> int:
        return sum(_msg_chars(m) for m in ms)

    while len(rest) > 1 and size(system) + size(rest) > budget_chars:
        rest.pop(0)
        while len(rest) > 1 and rest[0].get("role") == "tool":
            rest.pop(0)
    return system + rest


class OpenAiCompatibleProvider(Provider):
    """Any ``/v1/chat/completions`` endpoint (OpenAI, vLLM, NIM, LiteLLM, Ollama)."""

    provider_name = "openai_compatible"
    roles = ("general", "system1")

    # -- helpers ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Return the API root without a trailing slash."""
        return str(self.args.get("base_url") or "").rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self._secrets.get("api_key")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _client(self) -> Any:
        import httpx  # lazy: keeps import of this module cheap

        verify: Any = True
        ca_bundle = str(self.args.get("ca_bundle") or "").strip()
        if ca_bundle:
            verify = ca_bundle
        timeout = _float(self.args.get("timeout_s"), 60.0)
        return httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)), verify=verify
        )

    def _prepare(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        prompt = str(self.args.get("system_prompt") or "").strip()
        out: list[ChatMessage] = []
        if prompt:
            out.append({"role": "system", "content": prompt})
        out.extend(messages)
        return trim_to_context(
            out,
            _int(self.args.get("context_window"), 128000),
            _int(self.args.get("max_tokens"), 2048),
        )

    def _body(
        self,
        messages: list[ChatMessage],
        *,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": _int(self.args.get("max_tokens"), 2048),
            "temperature": _float(self.args.get("temperature"), 0.2),
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _raise_for_status(status: int, text: str) -> None:
        if status < 400:
            return
        message = text[:500]
        try:
            parsed = json.loads(text)
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                message = str(err.get("message") or message)
            elif isinstance(err, str):
                message = err
        except (json.JSONDecodeError, AttributeError):
            pass
        code = "auth" if status in (401, 403) else "http"
        raise ProviderError(code, f"HTTP {status}: {message}")

    def _translate(self, exc: Exception) -> ProviderError:
        import httpx

        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, httpx.TimeoutException):
            return ProviderError("timeout", f"Request timed out: {exc}")
        if isinstance(exc, httpx.HTTPError):
            return ProviderError("network", f"{type(exc).__name__}: {exc}")
        return ProviderError("protocol", f"{type(exc).__name__}: {exc}")

    # -- Provider API ----------------------------------------------------

    def probe(self) -> ProbeResult:
        """``GET /models`` then confirm the configured model is listed."""
        if not self.base_url:
            return ProbeResult(False, "config", "base_url is not set.")
        if not self.model:
            return ProbeResult(False, "config", "model is not set.")
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/models", headers=self._headers())
                self._raise_for_status(resp.status_code, resp.text)
                data = resp.json()
        except Exception as exc:
            err = self._translate(exc)
            return ProbeResult(False, err.code, str(err), {"base_url": self.base_url})
        ids = [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)]
        detail = {"base_url": self.base_url, "model": self.model, "models_visible": len(ids)}
        if ids and self.model not in ids:
            return ProbeResult(
                False,
                "model_not_found",
                f"Authenticated, but model '{self.model}' is not available to this key.",
                {**detail, "sample": ids[:10]},
            )
        return ProbeResult(True, "ok", f"Authenticated; model '{self.model}' available.", detail)

    def chat_stream(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]] | None = None
    ) -> Iterator[LlmEvent]:
        """Stream ``reasoning`` / ``delta`` / ``usage`` events.

        With ``tools``, streamed ``delta.tool_calls`` fragments are
        accumulated by index and emitted once, at the end, as an internal
        ``_tool_calls`` event (see :meth:`Provider.chat_stream`).
        """
        if not self.base_url or not self.model:
            raise ProviderError("config", "base_url and model must be set.")
        body = self._body(self._prepare(messages), stream=True, tools=tools)
        calls: dict[int, dict[str, str]] = {}
        try:
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={**self._headers(), "Accept": "text/event-stream"},
                    json=body,
                ) as resp,
            ):
                if resp.status_code >= 400:
                    resp.read()
                    self._raise_for_status(resp.status_code, resp.text)
                for line in resp.iter_lines():
                    yield from self._parse_sse_line(line, calls)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc
        if calls:
            yield {
                "type": "_tool_calls",
                "calls": [
                    {"id": c["id"] or f"call_{i}", "name": c["name"], "arguments": c["arguments"]}
                    for i, c in sorted(calls.items())
                    if c["name"]
                ],
            }

    @staticmethod
    def _parse_sse_line(
        line: str, calls: dict[int, dict[str, str]] | None = None
    ) -> Iterator[LlmEvent]:
        line = line.strip()
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(chunk, dict):
            return
        if isinstance(chunk.get("error"), (dict, str)):
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise ProviderError("http", str(msg))
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield {"type": "reasoning", "text": str(reasoning)}
            content = delta.get("content")
            if content:
                yield {"type": "delta", "text": str(content)}
            if calls is not None:
                for frag in delta.get("tool_calls") or []:
                    if not isinstance(frag, dict):
                        continue
                    idx = _int(frag.get("index"), len(calls))
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if frag.get("id"):
                        slot["id"] = str(frag["id"])
                    fn = frag.get("function") or {}
                    if isinstance(fn, dict):
                        name = str(fn.get("name") or "")
                        # Some gateways repeat the full name on every
                        # fragment, others split it; handle both.
                        if name and name != slot["name"]:
                            slot["name"] += name
                        if fn.get("arguments"):
                            slot["arguments"] += str(fn["arguments"])
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            yield {
                "type": "usage",
                "prompt_tokens": _int(usage.get("prompt_tokens"), 0),
                "completion_tokens": _int(usage.get("completion_tokens"), 0),
            }

    def decide(self, messages: list[ChatMessage]) -> Decision:
        """Ask the model for a strict-JSON decision (System 1 stop-gap)."""
        prepared = [{"role": "system", "content": _DECISION_INSTRUCTIONS}, *self._prepare(messages)]
        body = self._body(prepared, stream=False)
        try:
            with self._client() as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions", headers=self._headers(), json=body
                )
                self._raise_for_status(resp.status_code, resp.text)
                payload = resp.json()
        except Exception as exc:
            raise self._translate(exc) from exc
        try:
            content = str(payload["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("protocol", "Malformed chat completion.") from exc
        return parse_decision(content)


def parse_decision(content: str) -> Decision:
    """Parse a decision JSON object out of free model text.

    Tolerates code fences and leading prose. An unparsable reply is an
    out-of-scope, zero-confidence decision (so the router falls back).
    """
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return Decision(
            in_scope=False, decision="", confidence=0.0, rationale="System 1 reply was not JSON."
        )
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Decision(
            in_scope=False,
            decision="",
            confidence=0.0,
            rationale="System 1 reply was not valid JSON.",
        )
    if not isinstance(raw, dict):
        return Decision(in_scope=False, decision="", confidence=0.0)
    confidence = max(0.0, min(1.0, _float(raw.get("confidence"), 0.0)))
    action = raw.get("action")
    return Decision(
        in_scope=bool(raw.get("in_scope", True)),
        decision=str(raw.get("decision") or ""),
        confidence=confidence,
        action=str(action) if action else None,
        rationale=str(raw.get("rationale") or ""),
        answer=str(raw.get("answer") or ""),
    )


__all__ = ["OpenAiCompatibleProvider", "parse_decision", "trim_to_context"]
