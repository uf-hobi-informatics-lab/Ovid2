"""Executor entry points for the LLM runtime.

Two commands, both reachable as ``python -m ovid2.llm <cmd>`` (fast
path used by the web broker, ~0.2 s start-up) and as
``ovid2 tools test-llm`` / ``ovid2 tools llm-chat`` (operator CLI):

- ``probe <name>`` — load the connection + secret, run
  :meth:`Provider.probe`, print one JSON object.
- ``chat`` — read ``{"messages": [...], "route": {...}}`` from stdin and
  stream protocol events (see :mod:`ovid2.llm.protocol`) as JSON lines
  on stdout, flushing after every line. When ``route.tools`` is set the
  per-turn read-only API credentials come from ``OVID2_LLM_API_URL`` /
  ``OVID2_LLM_API_TOKEN`` (see :mod:`ovid2.llm.api_client`).

This module resolves secrets, so it must only ever run in the executor
trust domain (a subprocess), never inside the web process.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from ovid2.config import llms as _llms
from ovid2.llm.api_client import ReadOnlyApiClient
from ovid2.llm.protocol import LlmEvent, Provider
from ovid2.llm.providers import instantiate
from ovid2.llm.router import RouteConfig, route_chat, sanitize_messages

#: Upper bound on the stdin request body (bytes).
MAX_REQUEST_BYTES = 512 * 1024


def build_provider(name: str) -> Provider:
    """Load connection ``name`` (resolving its secret) and instantiate it."""
    config = _llms.load_llm_config(name)
    secrets = _llms.resolve_secrets(config)
    return instantiate(name, config, secrets)


def _emit(out: TextIO, event: LlmEvent) -> None:
    out.write(json.dumps(event, ensure_ascii=False) + "\n")
    out.flush()


def probe(name: str, out: TextIO = sys.stdout) -> int:
    """Probe one LLM connection and print a JSON result line."""
    result: dict[str, Any]
    try:
        result = build_provider(name).probe().to_dict()
    except (FileNotFoundError, PermissionError, ValueError, KeyError) as exc:
        result = {
            "ok": False,
            "code": "config",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
    except Exception as exc:
        result = {
            "ok": False,
            "code": "error",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
    _emit(out, result)
    return 0 if result.get("ok") else 1


def chat(stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    """Run one routed chat turn, streaming JSON-lines events."""
    raw = stdin.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _emit(out, {"type": "error", "code": "bad_request", "message": "Request too large."})
        _emit(out, {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0})
        return 1
    try:
        request = json.loads(raw or "{}")
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        _emit(out, {"type": "error", "code": "bad_request", "message": str(exc)})
        _emit(out, {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0})
        return 1
    messages = sanitize_messages(request.get("messages"))
    route = RouteConfig.from_dict(dict(request.get("route") or {}))
    ok = False
    try:
        client = ReadOnlyApiClient.from_env() if route.tools else None
        ui_context = request.get("ui_context")
        for event in route_chat(
            messages,
            route,
            build_provider,
            client,
            ui_context if isinstance(ui_context, dict) else None,
        ):
            if event.get("type") == "done":
                ok = bool(event.get("ok"))
            _emit(out, event)
    except Exception as exc:
        _emit(out, {"type": "error", "code": "error", "message": f"{type(exc).__name__}: {exc}"})
        _emit(out, {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0})
        return 1
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    """Return the ``python -m ovid2.llm`` argument parser."""
    parser = argparse.ArgumentParser(prog="python -m ovid2.llm", description="Ovid2 LLM executor.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="Probe an LLM connection.")
    p.add_argument("name")
    sub.add_parser("chat", help="Run one chat turn (request JSON on stdin).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        return probe(args.name)
    return chat()


__all__ = ["build_provider", "chat", "main", "probe"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
