"""Chat orchestration for the right-panel assistant (L1).

Takes the browser's conversation window, attaches the server-side
routing from ``settings.llm`` (the client can never pick a connection),
runs the turn in the executor via :mod:`llm_exec`, and frames the events
as Server-Sent Events.

Audit rows carry **metadata only** — actor, route, connection, model,
token counts, latency, outcome, and (with tools on) the name / HTTP
status / latency of every tool call and proposal, and the *name* of every
UI action (never its arguments). Prompt, answer and
tool-result text are never logged or persisted (they may contain PHI).

Assistant tools (L2): when ``settings.llm.tools_enabled`` is on, the
turn is offered the subset of the ``ovid2.agent_tools`` catalogue the
chatting user may use. A per-turn, GET-only token
(:mod:`ovid2.web.auth.llm_token`) carrying the user's role (capped at
``pipeline_operator``) is minted, passed to the executor child via its
environment, and revoked when the turn ends.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ovid2.agent_tools import AgentTool, load_catalogue
from ovid2.web import db as web_db
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Capability, Role, has_capability
from ovid2.web.models import LlmChatRequest
from ovid2.web.services import llm_exec, settings_svc

logger = logging.getLogger(__name__)

__all__ = ["ChatDisabledError", "ChatPrincipal", "allowed_tools", "build_request", "sse_stream"]


class ChatDisabledError(RuntimeError):
    """Raised when the assistant is disabled in settings."""


def _frame(event: dict[str, Any]) -> bytes:
    kind = str(event.get("type", "message"))
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {kind}\ndata: {data}\n\n".encode()


@dataclass(frozen=True)
class ChatPrincipal:
    """Who is chatting, and where the executor can reach the API.

    Attributes:
        user_id: ``AuthContext.user_id`` of the chatting user.
        role: Their role (the read token caps it at pipeline_operator).
        email: Their email when known.
        api_url: API root the executor should call back (loopback).
    """

    user_id: str
    role: Role
    email: str | None = None
    api_url: str | None = None


def allowed_tools(role: Role) -> list[AgentTool]:
    """Return the catalogue tools available to a user with ``role``.

    Read (GET) tools are filtered by the *capped* role the executor's
    token will carry; proposal and UI-action tools by the user's own
    role, since the user's browser session runs them.
    """
    capped = llm_token.cap_role(role)
    out: list[AgentTool] = []
    for tool in load_catalogue():
        effective = role if (tool.mutating or tool.ui_action) else capped
        try:
            cap = Capability(tool.capability)
        except ValueError:
            continue
        if has_capability(effective, cap):
            out.append(tool)
    return out


def build_request(body: LlmChatRequest, principal: ChatPrincipal | None = None) -> dict[str, Any]:
    """Return the executor stdin payload for one turn.

    Raises:
        ChatDisabledError: When ``settings.llm.enabled`` is False.
    """
    llm = settings_svc.get_settings().llm
    if not llm.enabled:
        raise ChatDisabledError("The assistant is disabled. Enable it under LLMs → Routing.")
    tools: list[str] = []
    if llm.tools_enabled and principal is not None and principal.api_url:
        tools = [t.name for t in allowed_tools(principal.role)]
    ui_context: dict[str, Any] | None = None
    if body.ui_context is not None and any(
        t.ui_action for t in load_catalogue() if t.name in tools
    ):
        ui_context = body.ui_context.model_dump()
    return {
        "messages": [m.model_dump() for m in body.messages],
        "ui_context": ui_context,
        "route": {
            "general": llm.general,
            "system1": llm.system1,
            "confidence_threshold": llm.confidence_threshold,
            "fallback_to_general": llm.fallback_to_general,
            "tools": tools,
            "max_tool_iterations": llm.max_tool_iterations,
            "tool_result_max_chars": llm.tool_result_max_chars,
        },
    }


def sse_stream(
    body: LlmChatRequest, *, actor: str, principal: ChatPrincipal | None = None
) -> Iterator[bytes]:
    """Yield SSE frames for one chat turn and audit its metadata.

    Args:
        body: The browser's conversation window.
        actor: Audit actor string.
        principal: The chatting user + API callback URL; required for tools.

    Raises:
        ChatDisabledError: Before the first frame when the assistant is off.
    """
    request = build_request(body, principal)
    llm = settings_svc.get_settings().llm
    token: str | None = None
    extra_env: dict[str, str] = {}
    if request["route"]["tools"] and principal is not None and principal.api_url:
        token = llm_token.mint(
            principal.user_id,
            principal.role,
            email=principal.email,
            ttl_seconds=float(llm.chat_timeout_seconds) + 5.0,
        )
        extra_env = {"OVID2_LLM_API_URL": principal.api_url, "OVID2_LLM_API_TOKEN": token}

    def _gen() -> Iterator[bytes]:
        meta: dict[str, Any] = {
            "route": "none",
            "connection": None,
            "model": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "ok": False,
            "elapsed_ms": 0,
            "error": None,
            "decision_accepted": None,
            "turns": len(body.messages),
            "tools_offered": len(request["route"]["tools"]),
            "tool_calls": [],
            "proposals": [],
            "ui_actions": [],
        }
        yield b": connected\n\n"
        try:
            for event in llm_exec.chat_stream(
                request,
                timeout=float(llm.chat_timeout_seconds),
                max_concurrent=llm.max_concurrent_chats,
                extra_env=extra_env,
            ):
                kind = event.get("type")
                if kind == "meta":
                    meta.update(
                        route=event.get("route"),
                        connection=event.get("connection"),
                        model=event.get("model"),
                    )
                elif kind == "usage":
                    meta["prompt_tokens"] += int(event.get("prompt_tokens") or 0)
                    meta["completion_tokens"] += int(event.get("completion_tokens") or 0)
                elif kind == "decision":
                    meta["decision_accepted"] = bool(event.get("accepted"))
                elif kind == "error":
                    meta["error"] = event.get("code")
                elif kind == "done":
                    meta.update(
                        ok=bool(event.get("ok")), elapsed_ms=int(event.get("elapsed_ms") or 0)
                    )
                elif kind == "tool_result":
                    entry: dict[str, Any] = {
                        "name": event.get("name"),
                        "status": event.get("status"),
                        "ms": event.get("elapsed_ms"),
                    }
                    if event.get("rejected") or not event.get("ok"):
                        entry["summary"] = str(event.get("summary") or "")[:200]
                    meta["tool_calls"].append(entry)
                elif kind in ("proposal", "open_draft"):
                    prop: dict[str, Any] = {
                        "name": event.get("name"),
                        "method": event.get("method"),
                        "path": event.get("path"),
                    }
                    if kind == "open_draft":
                        prop["draft"] = True
                    meta["proposals"].append(prop)
                elif kind == "ui_action":
                    # Name only: the arguments are cosmetic user state, not audited.
                    meta["ui_actions"].append({"name": event.get("name")})
                elif kind == "_tool_calls":
                    continue  # internal; never relayed
                yield _frame(event)
        finally:
            llm_token.revoke(token)
            try:
                web_db.ensure_schema()
                web_db.audit(
                    actor=actor,
                    action="llm.chat",
                    target=str(meta.get("connection") or "none"),
                    detail_json=json.dumps(meta),
                )
            except Exception:
                logger.exception("Failed to audit llm.chat")

    return _gen()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web services.")
