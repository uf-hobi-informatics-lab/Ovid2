"""Chat orchestration for the right-panel assistant (L1).

Takes the new message(s) of a turn, prepends the saved history of the
turn's *session* (:mod:`llm_sessions_svc`), attaches the server-side
routing from ``settings.llm`` (the client can never pick a connection),
runs the turn in the executor via :mod:`llm_exec`, and frames the events
as Server-Sent Events. When the turn ends the user message and the reply
(with its display metadata) are appended to the session; after the first
turn of a session a background thread asks the general model for a short
title (``python -m ovid2.llm title``), keeping a prompt-derived fallback
on failure.

Audit rows carry **metadata only** — actor, session id, route,
connection, model, token counts, latency, outcome, and (with tools on)
the name / HTTP status / latency of every tool call and proposal, and the
*name* of every UI action (never its arguments). Prompt, answer and
tool-result text are never logged or audited (they may contain PHI); they
are stored only in the user's session rows.

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
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ovid2.agent_tools import AgentTool, load_catalogue
from ovid2.web import db as web_db
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Capability, Role, has_capability
from ovid2.web.models import LlmChatRequest
from ovid2.web.services import llm_exec, llm_sessions_svc, llms_svc, settings_svc
from ovid2.workspaces import active_workspace

logger = logging.getLogger(__name__)

__all__ = [
    "ChatDisabledError",
    "ChatPrincipal",
    "allowed_tools",
    "build_request",
    "open_session",
    "sse_stream",
]

#: Trailing window of messages sent to the model each turn.
MAX_TURNS = 40
#: Wall-clock ceiling for the background title call.
TITLE_TIMEOUT_SECONDS = 45.0


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


def build_request(
    body: LlmChatRequest,
    principal: ChatPrincipal | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return the executor stdin payload for one turn.

    Args:
        body: The browser's request (the new messages of this turn).
        principal: The chatting user; required for tools.
        history: Saved session messages to prepend (``None`` = none).

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
    new = [{"role": m.role, "content": m.content} for m in body.messages]
    messages = [*(history or []), *new][-MAX_TURNS:]
    return {
        "messages": messages,
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


def open_session(
    body: LlmChatRequest, principal: ChatPrincipal
) -> tuple[str, list[dict[str, str]]]:
    """Resolve (or create) the turn's session and return ``(id, history)``.

    Raises:
        llm_sessions_svc.SessionNotFoundError: When ``body.session_id`` is
            not one of the principal's sessions.
    """
    if body.session_id:
        return body.session_id, llm_sessions_svc.history_for_turn(
            principal.user_id, body.session_id, MAX_TURNS
        )
    return llm_sessions_svc.create_session(principal.user_id).session_id, []


def _generate_title(session_id: str, connection: str | None, prompt: str, reply: str) -> None:
    """Ask the general model for a title; keep the fallback on any failure."""
    title, source = "", "fallback"
    if connection:
        try:
            result = llm_exec.run_title(
                {"connection": connection, "prompt": prompt, "reply": reply},
                timeout=TITLE_TIMEOUT_SECONDS,
            )
            if result.get("ok") and str(result.get("title") or "").strip():
                title, source = str(result["title"]), "llm"
        except llm_exec.ExecError:
            logger.info("Session title generation failed; keeping the fallback title.")
    try:
        llm_sessions_svc.set_generated_title(
            session_id, title or llm_sessions_svc.fallback_title(prompt), source
        )
    except Exception:
        logger.exception("Failed to store the session title")


def _start_titler(session_id: str, connection: str | None, prompt: str, reply: str) -> None:
    thread = threading.Thread(
        target=_generate_title,
        args=(session_id, connection, prompt, reply),
        name=f"llm-title-{session_id[:8]}",
        daemon=True,
    )
    thread.start()


def sse_stream(
    body: LlmChatRequest, *, actor: str, principal: ChatPrincipal | None = None
) -> Iterator[bytes]:
    """Yield SSE frames for one chat turn, save it to its session, and audit it.

    With a ``principal`` the turn is saved to a session (created when
    ``body.session_id`` is absent): the new user message(s) plus the
    assistant reply with its display metadata. A final ``session`` event
    carries the session id, title, token counters and context window.

    Args:
        body: The new messages of this turn (+ optional session id).
        actor: Audit actor string.
        principal: The chatting user + API callback URL.

    Raises:
        ChatDisabledError: Before the first frame when the assistant is off.
        llm_sessions_svc.SessionNotFoundError: Before the first frame when
            ``body.session_id`` is not the principal's.
    """
    llm = settings_svc.get_settings().llm
    if not llm.enabled:
        raise ChatDisabledError("The assistant is disabled. Enable it under LLMs → Routing.")
    session_id: str | None = None
    history: list[dict[str, str]] = []
    if principal is not None:
        session_id, history = open_session(body, principal)
    request = build_request(body, principal, history)
    first_turn = not history
    token: str | None = None
    extra_env: dict[str, str] = {}
    if request["route"]["tools"] and principal is not None and principal.api_url:
        token = llm_token.mint(
            principal.user_id,
            principal.role,
            email=principal.email,
            ttl_seconds=float(llm.chat_timeout_seconds) + 5.0,
            workspace_id=active_workspace(),
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
            "turns": len(request["messages"]),
            "tools_offered": len(request["route"]["tools"]),
            "tool_calls": [],
            "proposals": [],
            "ui_actions": [],
            "session_id": session_id,
        }
        # What the browser renders for the reply (saved with the message).
        reply_text: list[str] = []
        view: dict[str, Any] = {
            "notices": [],
            "reasoning": "",
            "tools": [],
            "proposals": [],
            "uiActions": [],
        }
        context_tokens = 0
        saved = False

        def _persist(done: bool) -> dict[str, Any] | None:
            nonlocal saved
            if saved or principal is None or session_id is None:
                return None
            saved = True
            view.update(
                route=meta["route"],
                model=meta["model"],
                connection=meta["connection"],
                elapsed_ms=meta["elapsed_ms"],
                prompt_tokens=meta["prompt_tokens"],
                completion_tokens=meta["completion_tokens"],
            )
            if meta["error"] and "error" not in view:
                view["error"] = meta["error"]
            if not done:
                view["incomplete"] = True
                view["notices"].append("Stopped.")
            turn: list[dict[str, Any]] = []
            for m in body.messages:
                item: dict[str, Any] = {"role": m.role, "content": m.content}
                if m.kind:
                    item["meta"] = {"kind": m.kind}
                turn.append(item)
            turn.append({"role": "assistant", "content": "".join(reply_text), "meta": view})
            try:
                summary = llm_sessions_svc.append_turn(
                    principal.user_id,
                    session_id,
                    turn,
                    prompt_tokens=int(meta["prompt_tokens"]),
                    completion_tokens=int(meta["completion_tokens"]),
                    context_tokens=context_tokens or None,
                    connection=meta["connection"],
                    model=meta["model"],
                )
            except Exception:
                logger.exception("Failed to save the assistant turn")
                return None
            if first_turn and summary.title_source == "pending":
                prompt = body.messages[0].content
                llm_sessions_svc.set_provisional_title(
                    session_id, llm_sessions_svc.fallback_title(prompt)
                )
                general = request["route"]["general"]
                if done and general and meta["route"] != "none":
                    _start_titler(session_id, general, prompt, "".join(reply_text))
                else:
                    llm_sessions_svc.set_generated_title(
                        session_id, llm_sessions_svc.fallback_title(prompt), "fallback"
                    )
            detail = llm_sessions_svc.get_session(principal.user_id, session_id)
            return {
                "type": "session",
                "session_id": session_id,
                "title": detail.title,
                "title_source": detail.title_source,
                "last_prompt_tokens": detail.last_prompt_tokens,
                "total_prompt_tokens": detail.total_prompt_tokens,
                "total_completion_tokens": detail.total_completion_tokens,
                "context_window": llms_svc.context_window(meta["connection"] or llm.general),
                "assistant_seq": detail.message_count,
            }

        yield b": connected\n\n"
        if session_id is not None:
            yield _frame({"type": "session_open", "session_id": session_id})
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
                elif kind == "delta":
                    reply_text.append(str(event.get("text") or ""))
                elif kind == "reasoning":
                    view["reasoning"] += str(event.get("text") or "")
                elif kind == "notice":
                    view["notices"].append(str(event.get("message") or ""))
                elif kind == "usage":
                    pt = int(event.get("prompt_tokens") or 0)
                    meta["prompt_tokens"] += pt
                    meta["completion_tokens"] += int(event.get("completion_tokens") or 0)
                    context_tokens = max(
                        context_tokens, pt + int(event.get("completion_tokens") or 0)
                    )
                elif kind == "decision":
                    meta["decision_accepted"] = bool(event.get("accepted"))
                    view["decision"] = event
                elif kind == "error":
                    meta["error"] = event.get("code")
                    view["error"] = str(event.get("message") or event.get("code") or "error")
                elif kind == "done":
                    meta.update(
                        ok=bool(event.get("ok")), elapsed_ms=int(event.get("elapsed_ms") or 0)
                    )
                    if not meta["route"] or meta["route"] == "none":
                        meta["route"] = event.get("route")
                elif kind == "tool_call":
                    view["tools"].append(
                        {
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "method": event.get("method"),
                            "path": event.get("path"),
                            "arguments": event.get("arguments"),
                        }
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
                    hit = next((t for t in view["tools"] if t.get("id") == event.get("id")), None)
                    if hit is None:
                        hit = {"id": event.get("id"), "name": event.get("name")}
                        view["tools"].append(hit)
                    hit["result"] = {
                        "status": event.get("status"),
                        "ok": event.get("ok"),
                        "elapsed_ms": event.get("elapsed_ms"),
                        "summary": event.get("summary"),
                        "preview": str(event.get("preview") or "")[:4000],
                        "rejected": bool(event.get("rejected")),
                        "duplicate": bool(event.get("duplicate")),
                    }
                elif kind in ("proposal", "open_draft"):
                    prop: dict[str, Any] = {
                        "name": event.get("name"),
                        "method": event.get("method"),
                        "path": event.get("path"),
                    }
                    if kind == "open_draft":
                        prop["draft"] = True
                    meta["proposals"].append(prop)
                    view["proposals"].append(
                        {
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "method": event.get("method"),
                            "path": event.get("path"),
                            "body": event.get("body"),
                            "state": "running" if kind == "open_draft" else "pending",
                            **({"draft": True} if kind == "open_draft" else {}),
                        }
                    )
                elif kind == "ui_action":
                    # Name only: the arguments are cosmetic user state, not audited.
                    meta["ui_actions"].append({"name": event.get("name")})
                    view["uiActions"].append(
                        {
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "args": event.get("args") or {},
                            "state": "pending",
                        }
                    )
                elif kind == "_tool_calls":
                    continue  # internal; never relayed
                yield _frame(event)
                if kind == "done":
                    session_event = _persist(done=True)
                    if session_event is not None:
                        yield _frame(session_event)
        finally:
            llm_token.revoke(token)
            _persist(done=False)
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
