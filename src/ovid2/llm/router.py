"""Dual-system router: System 1 decides first, the general LLM answers the rest.

Flow for one chat turn::

    system1 configured? ── no ──────────────────────────────┐
        │ yes                                               │
        ▼                                                   │
    decide(messages) ── error ─ notice ─────────────────────┤
        │      └─ not_implemented (stub) ─ silent ─────────┤
        │                                                   │
        ▼                                                   │
    in_scope and confidence >= threshold?                   │
        │ yes → emit decision(accepted) + answer → done     │
        │ no  → emit decision(rejected) ────────────────────┤
                                                            ▼
                                  fallback_to_general and general configured?
                                        │ yes → stream general → done
                                        │ no  → error(no_route)

The router never executes a System 1 ``action``. When the web tier
enables assistant tools (``route.tools``), the general LLM runs inside
:func:`ovid2.llm.tool_loop.run_tool_loop`: read-only ``GET`` tools are
executed against the Ovid2 API with a per-turn token, and write tools
are surfaced as ``proposal`` events for the human to run.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from ovid2.llm.api_client import ReadOnlyApiClient
from ovid2.llm.protocol import ChatMessage, LlmEvent, Provider, ProviderError
from ovid2.llm.tool_loop import TOOL_PROMPT, ToolContext, run_tool_loop, ui_context_prompt

_LOG = logging.getLogger(__name__)

#: Built-in system prompt for the general LLM. Teaches the model the
#: canvas "answer block" format the right-panel renderer understands.
BASE_SYSTEM_PROMPT = (
    "You are the Ovid2 assistant inside the Ovid2 ETL control panel "
    "(PySpark ETL for PCORnet / OMOP clinical data). Be concise and accurate; "
    "say so when you are unsure. Answer in Markdown. When structured output helps, you may add "
    "canvas blocks as fenced code with the language tag `ovid-block` "
    "containing one JSON object, one of: "
    '{"type":"table","title":str,"columns":[str],"rows":[[...]]}, '
    '{"type":"kpi-row","items":[{"label":str,"value":str}]}, '
    '{"type":"status-list","items":[{"label":str,"status":"ok|warn|error","detail":str}]}.'
)


def today_prompt(today: date | None = None) -> str:
    """Return the system-prompt sentence stating today's date.

    Models have no clock; without this they invent a ``batch_id`` such
    as a past ``YYYYMMDD`` when told "use today's date".

    Args:
        today: Override for tests; defaults to the host's local date.
    """
    d = today or date.today()
    return (
        f" Today's date is {d.isoformat()} ({d.strftime('%A')}); when a run needs a "
        f"batch_id and the user gives none, use {d.strftime('%Y%m%d')}."
    )


@dataclass(frozen=True)
class RouteConfig:
    """Routing knobs for one chat turn (sent by the web tier).

    Attributes:
        general: General LLM connection name, or ``None``.
        system1: System 1 connection name, or ``None``.
        confidence_threshold: Minimum System 1 confidence to accept.
        fallback_to_general: Whether rejected decisions fall through.
        tools: Agent tool names allowed this turn (empty → chat-only).
        max_tool_iterations: Upper bound on tool-loop model round-trips.
        tool_result_max_chars: Cap on one tool result fed to the model.
    """

    general: str | None = None
    system1: str | None = None
    confidence_threshold: float = 0.7
    fallback_to_general: bool = True
    tools: tuple[str, ...] = ()
    max_tool_iterations: int = 6
    tool_result_max_chars: int = 8000

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RouteConfig:
        """Build from the stdin request payload."""
        try:
            threshold = float(raw.get("confidence_threshold", 0.7))
        except (TypeError, ValueError):
            threshold = 0.7
        return cls(
            general=(str(raw["general"]) if raw.get("general") else None),
            system1=(str(raw["system1"]) if raw.get("system1") else None),
            confidence_threshold=max(0.0, min(1.0, threshold)),
            fallback_to_general=bool(raw.get("fallback_to_general", True)),
            tools=tuple(str(t) for t in (raw.get("tools") or []) if isinstance(t, str)),
            max_tool_iterations=max(1, min(20, _int(raw.get("max_tool_iterations"), 6))),
            tool_result_max_chars=max(
                256, min(200000, _int(raw.get("tool_result_max_chars"), 8000))
            ),
        )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: Factory signature: connection name → provider instance.
ProviderFactory = Callable[[str], Provider]


def sanitize_messages(
    raw: Any, *, max_messages: int = 40, max_chars: int = 32000
) -> list[ChatMessage]:
    """Keep only well-formed user/assistant turns, bounded in count and size."""
    out: list[ChatMessage] = []
    if not isinstance(raw, list):
        return out
    for item in raw[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if content.strip():
            out.append({"role": role, "content": content[:max_chars]})
    return out


def route_chat(
    messages: list[ChatMessage],
    route: RouteConfig,
    factory: ProviderFactory,
    api_client: ReadOnlyApiClient | None = None,
    ui_context: dict[str, Any] | None = None,
) -> Iterator[LlmEvent]:
    """Run one chat turn and yield protocol events (always ends with ``done``).

    Args:
        messages: Sanitised user/assistant turns.
        route: Routing + tool knobs.
        factory: Connection name → provider.
        api_client: Read-only Ovid2 API client for GET tools (``None`` →
            only proposal / UI-action tools are offered).
        ui_context: The browser's current look (theme + overrides), added
            to the system prompt only when a UI-action tool is offered.
    """
    started = time.monotonic()

    def done(route_name: str, ok: bool) -> LlmEvent:
        return {
            "type": "done",
            "route": route_name,
            "ok": ok,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    if not messages or messages[-1]["role"] != "user":
        yield {
            "type": "error",
            "code": "bad_request",
            "message": "The conversation must end with a user message.",
        }
        yield done("none", False)
        return

    # ---- System 1 -----------------------------------------------------
    if route.system1:
        try:
            s1 = factory(route.system1)
            threshold = route.confidence_threshold
            override = s1.args.get("confidence_threshold")
            if override not in (None, ""):
                with contextlib.suppress(TypeError, ValueError):
                    threshold = max(0.0, min(1.0, float(override)))
            decision = s1.decide(messages)
            accepted = decision.in_scope and decision.confidence >= threshold
            yield decision.to_event(accepted=accepted, connection=route.system1)
            if accepted:
                yield {
                    "type": "meta",
                    "route": "system1",
                    "connection": route.system1,
                    "model": s1.model,
                }
                text = decision.answer or decision.decision
                if text:
                    yield {"type": "delta", "text": text}
                yield done("system1", True)
                return
            yield {
                "type": "notice",
                "message": (
                    f"System 1 deferred (confidence "
                    f"{decision.confidence:.2f} < {threshold:.2f}"
                    f"{'' if decision.in_scope else ', out of scope'})."
                ),
            }
        except NotImplementedError as exc:
            # A stub provider (e.g. JEV before it ships) is an expected,
            # permanent condition — fall through to the general LLM silently
            # instead of cluttering every chat turn with a notice.
            _LOG.debug("System 1 %s unavailable: %s", route.system1, exc)
        except (ProviderError, FileNotFoundError, ValueError, KeyError, PermissionError) as exc:
            yield {"type": "notice", "message": f"System 1 failed: {exc}"}
        if not route.fallback_to_general:
            yield {
                "type": "error",
                "code": "no_route",
                "message": "System 1 did not accept and fallback is disabled.",
            }
            yield done("system1", False)
            return

    # ---- General ------------------------------------------------------
    if not route.general:
        yield {
            "type": "error",
            "code": "no_route",
            "message": "No general LLM is configured. Pick one under LLMs → Routing.",
        }
        yield done("none", False)
        return
    try:
        general = factory(route.general)
    except (FileNotFoundError, ValueError, KeyError, PermissionError) as exc:
        yield {"type": "error", "code": "config", "message": str(exc)}
        yield done("general", False)
        return
    yield {"type": "meta", "route": "general", "connection": route.general, "model": general.model}
    ctx = (
        ToolContext(
            route.tools,
            api_client,
            max_iterations=route.max_tool_iterations,
            result_max_chars=route.tool_result_max_chars,
        )
        if route.tools
        else None
    )
    prompt = (
        BASE_SYSTEM_PROMPT
        + today_prompt()
        + (TOOL_PROMPT if ctx and ctx.tools else " You have no access to live Ovid2 data or tools.")
    )
    if ctx and any(t.ui_action for t in ctx.tools):
        prompt += ui_context_prompt(ui_context)
    convo: list[ChatMessage] = [{"role": "system", "content": prompt}, *messages]
    try:
        if ctx and ctx.tools:
            yield from run_tool_loop(general, convo, ctx)
        else:
            for event in general.chat_stream(convo):
                if event.get("type") != "_tool_calls":
                    yield event
    except NotImplementedError as exc:
        yield {"type": "error", "code": "not_implemented", "message": str(exc)}
        yield done("general", False)
        return
    except ProviderError as exc:
        yield {"type": "error", "code": exc.code, "message": str(exc)}
        yield done("general", False)
        return
    yield done("general", True)


__all__ = [
    "BASE_SYSTEM_PROMPT",
    "ProviderFactory",
    "RouteConfig",
    "route_chat",
    "sanitize_messages",
]
