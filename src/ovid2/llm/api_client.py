"""Read-only HTTP client the LLM executor uses to call the Ovid2 API.

The tool loop (:mod:`ovid2.llm.tool_loop`) runs inside the ``python -m
ovid2.llm chat`` child. It reaches Ovid2 state **only** through the
typed ``/api/v1`` capability API, exactly like the MCP agent tier —
never by importing services, drivers or secrets.

Credentials: the web tier mints a short-lived, GET-only bearer token for
the chatting user (``ovid2.web.auth.llm_token``) and passes it in the
child's environment:

- ``OVID2_LLM_API_URL``   — API root, e.g. ``http://127.0.0.1:8090``
- ``OVID2_LLM_API_TOKEN`` — the per-turn token

This client refuses any method other than ``GET`` before a byte leaves
the process (defence in depth: the API rejects them too).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

__all__ = ["ApiResult", "ReadOnlyApiClient", "ReadOnlyViolationError"]

ENV_URL = "OVID2_LLM_API_URL"
ENV_TOKEN = "OVID2_LLM_API_TOKEN"  # noqa: S105 - env var name, not a secret


class ReadOnlyViolationError(RuntimeError):
    """Raised when code asks this client for a non-GET request."""


@dataclass(frozen=True)
class ApiResult:
    """Outcome of one API call.

    Attributes:
        status: HTTP status (0 when the request never completed).
        ok: ``200 <= status < 300``.
        text: Response body (JSON pretty-compacted when parseable).
        elapsed_ms: Wall-clock latency.
    """

    status: int
    ok: bool
    text: str
    elapsed_ms: int


class ReadOnlyApiClient:
    """Tiny synchronous ``httpx`` wrapper restricted to ``GET /api/v1/...``."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> ReadOnlyApiClient | None:
        """Build from ``OVID2_LLM_API_URL`` / ``OVID2_LLM_API_TOKEN`` (or ``None``)."""
        url = os.environ.get(ENV_URL, "").strip()
        token = os.environ.get(ENV_TOKEN, "").strip()
        if not url or not token:
            return None
        return cls(url, token)

    def request(self, method: str, path: str, query: dict[str, Any] | None = None) -> ApiResult:
        """Issue one request.

        Raises:
            ReadOnlyViolationError: For any method other than ``GET`` or a path
                outside ``/api/v1/``.
        """
        if method.upper() != "GET":
            raise ReadOnlyViolationError(f"assistant executor may not {method} {path}")
        if not path.startswith("/api/v1/") or ".." in path.split("?")[0].split("/"):
            raise ReadOnlyViolationError(f"assistant executor may not call {path}")
        import httpx  # lazy

        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self._timeout, connect=5.0), follow_redirects=False
            ) as client:
                resp = client.get(
                    self.base_url + path,
                    params=query or None,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json, text/plain",
                    },
                )
        except httpx.HTTPError as exc:
            return ApiResult(
                0, False, f"{type(exc).__name__}: {exc}", int((time.monotonic() - started) * 1000)
            )
        text = resp.text
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            text = json.dumps(resp.json(), ensure_ascii=False, separators=(",", ":"))
        return ApiResult(
            resp.status_code,
            200 <= resp.status_code < 300,
            text,
            int((time.monotonic() - started) * 1000),
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("api_client is a library module; not runnable directly.")
