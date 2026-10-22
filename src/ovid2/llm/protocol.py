"""Provider contract + event vocabulary for the dual-system LLM runtime.

Event vocabulary (one JSON object per line on the executor's stdout,
relayed verbatim to the browser as Server-Sent Events by
``ovid2.web.services.llm_chat_svc``):

==============  ==========================================================
``type``        payload
==============  ==========================================================
``meta``        ``route`` (``general``/``system1``), ``connection``, ``model``
``notice``      ``message`` — non-fatal information (e.g. System 1 absent)
``decision``    ``decision``, ``confidence``, ``action``, ``rationale``,
                ``in_scope``, ``accepted``, ``connection``
``reasoning``   ``text`` — model "thinking" tokens, when the model emits them
``delta``       ``text`` — answer tokens
``usage``       ``prompt_tokens``, ``completion_tokens``
``tool_call``   ``id``, ``name``, ``arguments`` (dict), ``method``, ``path``
                — a read-only tool the executor is about to call
``tool_result`` ``id``, ``name``, ``status`` (HTTP), ``ok``, ``elapsed_ms``,
                ``summary``, ``truncated``, ``rejected`` (rejected before
                any HTTP call — unknown tool / invalid arguments)
``proposal``    ``id``, ``name``, ``description``, ``method``, ``path``,
                ``body`` — a write the *human* must confirm and run
``open_draft``  same payload as ``proposal`` — a draft-job creation the
                browser runs immediately, then opens the draft's run page
                (nothing executes until the human presses ▶ there)
``ui_action``   ``id``, ``name``, ``description``, ``args`` — a cosmetic,
                browser-only change (e.g. page theme). No HTTP call; the
                browser re-validates ``args``, applies them, offers Undo
``error``       ``code``, ``message``
``done``        ``route``, ``elapsed_ms``, ``ok``
==============  ==========================================================
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

#: A chat message: ``{"role": "user"|"assistant"|"system"|"tool", "content": str}``
#: plus, for tool use, ``tool_calls`` (assistant) / ``tool_call_id`` (tool).
ChatMessage = dict[str, Any]

#: One streamed event (see module docstring).
LlmEvent = dict[str, Any]

EVENT_TYPES: frozenset[str] = frozenset(
    {"meta", "notice", "decision", "reasoning", "delta", "usage", "error", "done",
     "tool_call", "tool_result", "proposal", "open_draft",
     "ui_action"}
)


class ProviderError(RuntimeError):
    """Raised for transport / protocol failures talking to a provider.

    Attributes:
        code: Short machine code (``auth``, ``http``, ``timeout``,
            ``network``, ``protocol``, ``config``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Decision:
    """A System 1 decision.

    Attributes:
        in_scope: Whether System 1 considers the request its own.
        decision: Short decision label / answer (free text).
        confidence: 0.0–1.0 self-reported confidence.
        action: Optional machine-readable action name (never executed in v1).
        rationale: Short human-readable justification.
        answer: Optional user-facing answer text.
    """

    in_scope: bool
    decision: str
    confidence: float
    action: str | None = None
    rationale: str = ""
    answer: str = ""

    def to_event(self, *, accepted: bool, connection: str) -> LlmEvent:
        """Render as a ``decision`` event."""
        payload = asdict(self)
        payload.update({"type": "decision", "accepted": accepted, "connection": connection})
        return payload


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of :meth:`Provider.probe` (mirrors the driver probe shape)."""

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {"ok": self.ok, "code": self.code, "message": self.message,
                "detail": dict(self.detail)}


class Provider(abc.ABC):
    """One LLM wire protocol.

    Subclasses are constructed with the connection's non-secret ``args``
    and the resolved ``secrets`` payload. ``provider_name`` must match
    the key in :data:`ovid2.llm.providers.PROVIDER_TYPE_MAP` and in
    :data:`ovid2.config.llms._KNOWN_PROVIDERS`.
    """

    provider_name: str = ""
    #: Roles this provider can fill (mirrors ``config.llms._PROVIDER_ROLES``).
    roles: tuple[str, ...] = ()

    def __init__(self, name: str, args: dict[str, Any], secrets: dict[str, Any]) -> None:
        self.name = name
        self.args = dict(args)
        self._secrets = dict(secrets)

    @property
    def model(self) -> str:
        """Return the configured model id (may be empty)."""
        return str(self.args.get("model") or "")

    @abc.abstractmethod
    def probe(self) -> ProbeResult:
        """Run a cheap authenticated round-trip."""

    def chat_stream(self, messages: list[ChatMessage],
                    tools: list[dict[str, Any]] | None = None) -> Iterator[LlmEvent]:
        """Stream a general answer as ``reasoning`` / ``delta`` / ``usage`` events.

        When ``tools`` (OpenAI function-calling schema) is given and the
        model decides to call some, the stream additionally yields one
        internal ``{"type": "_tool_calls", "calls": [{"id", "name",
        "arguments"}]}`` event at the end. The tool loop consumes it; it
        is never relayed to the browser.

        Raises:
            NotImplementedError: When the provider cannot act as a general LLM.
            ProviderError: On transport failure.
        """
        raise NotImplementedError(f"{self.provider_name} cannot answer general questions.")

    def decide(self, messages: list[ChatMessage]) -> Decision:
        """Return a System 1 decision for the conversation.

        Raises:
            NotImplementedError: When the provider cannot act as System 1.
            ProviderError: On transport failure.
        """
        raise NotImplementedError(f"{self.provider_name} cannot make System 1 decisions.")


__all__ = [
    "EVENT_TYPES",
    "ChatMessage",
    "Decision",
    "LlmEvent",
    "ProbeResult",
    "Provider",
    "ProviderError",
]
