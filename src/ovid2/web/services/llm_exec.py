"""Executor-subprocess launcher for the LLM runtime (L1).

The web tier never resolves the LLM API key. Probes and chat turns run
in a short-lived ``python -m ovid2.llm …`` subprocess (the executor
trust domain), which reads the secret, calls the provider, and writes
JSON lines on stdout. This module owns:

- the allow-listed child environment (:data:`WORKER_ENV_ALLOWLIST`, the
  same list the job worker uses), so ambient credentials in the web
  process's environment are not inherited;
- a process-wide concurrency cap for chat turns;
- a wall-clock watchdog that kills a hung child;
- cleanup when the HTTP client disconnects mid-stream.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import Any

from ovid2.web.settings import WORKER_ENV_ALLOWLIST

__all__ = ["ExecError", "chat_stream", "child_env", "run_probe"]


class ExecError(RuntimeError):
    """Raised when the executor subprocess could not produce a result."""


_SLOTS_LOCK = threading.Lock()
_ACTIVE = 0


def _allowed(key: str) -> bool:
    for pattern in WORKER_ENV_ALLOWLIST:
        if pattern.endswith("*"):
            if key.startswith(pattern[:-1]):
                return True
        elif key == pattern:
            return True
    return False


def child_env() -> dict[str, str]:
    """Return the allow-listed environment for an LLM executor child."""
    env = {k: v for k, v in os.environ.items() if _allowed(k)}
    # The MCP service token is never needed by the LLM executor.
    env.pop("OVID2_MCP_API_TOKEN", None)
    # Never inherit an ambient assistant token; chat_stream injects a fresh
    # per-turn one explicitly.
    env.pop("OVID2_LLM_API_TOKEN", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _argv(*args: str) -> list[str]:
    return [sys.executable, "-m", "ovid2.llm", *args]


def run_probe(name: str, *, timeout: float) -> dict[str, Any]:
    """Run ``python -m ovid2.llm probe <name>`` and return its JSON result."""
    try:
        completed = subprocess.run(
            _argv("probe", name), capture_output=True, text=True, timeout=timeout,
            check=False, env=child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecError(f"LLM probe timed out after {timeout:.0f}s.") from exc
    except OSError as exc:
        raise ExecError(f"Could not launch LLM executor: {exc}") from exc
    for line in reversed((completed.stdout or "").strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ExecError(f"LLM executor returned no JSON (exit={completed.returncode}). "
                    f"stderr tail: {(completed.stderr or '')[-400:]}")


def _acquire(limit: int) -> bool:
    global _ACTIVE
    with _SLOTS_LOCK:
        if limit <= _ACTIVE:
            return False
        _ACTIVE += 1
        return True


def _release() -> None:
    global _ACTIVE
    with _SLOTS_LOCK:
        _ACTIVE = max(0, _ACTIVE - 1)


def chat_stream(request: dict[str, Any], *, timeout: float,
                max_concurrent: int,
                extra_env: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """Run one chat turn in the executor and yield its JSON-line events.

    Always terminates with a ``done`` event, even when the child fails.

    Args:
        request: ``{"messages": [...], "route": {...}}`` written to stdin.
        timeout: Wall-clock ceiling; the child is killed when exceeded.
        max_concurrent: Process-wide cap on simultaneous chat children.
        extra_env: Additional child env (the per-turn ``OVID2_LLM_API_URL``
            / ``OVID2_LLM_API_TOKEN`` when assistant tools are on).
    """
    if not _acquire(max_concurrent):
        yield {"type": "error", "code": "busy",
               "message": "The assistant is busy; try again in a moment."}
        yield {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0}
        return
    proc: subprocess.Popen[str] | None = None
    timed_out = threading.Event()
    saw_done = False
    try:
        try:
            proc = subprocess.Popen(
                _argv("chat"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                env={**child_env(), **(extra_env or {})},
            )
        except OSError as exc:
            yield {"type": "error", "code": "unreachable",
                   "message": f"Could not launch LLM executor: {exc}"}
            yield {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0}
            return

        def _kill() -> None:
            timed_out.set()
            if proc is not None and proc.poll() is None:
                proc.kill()

        watchdog = threading.Timer(timeout, _kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps(request))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or "type" not in event:
                    continue
                if event["type"] == "done":
                    saw_done = True
                yield event
            proc.wait(timeout=5)
        finally:
            watchdog.cancel()
        if not saw_done:
            if timed_out.is_set():
                yield {"type": "error", "code": "timeout",
                       "message": f"The assistant timed out after {timeout:.0f}s."}
            else:
                tail = ""
                if proc.stderr is not None:
                    try:
                        tail = proc.stderr.read()[-400:]
                    except (OSError, ValueError):
                        tail = ""
                yield {"type": "error", "code": "error",
                       "message": f"LLM executor exited unexpectedly. {tail}".strip()}
            yield {"type": "done", "route": "none", "ok": False, "elapsed_ms": 0}
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        _release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web services.")
