"""Thin async HTTP client used by every MCP tool.

Every tool receives a :class:`WebApiClient` and forwards its arguments
to the upstream FastAPI backend. Keeping the client here (rather than
in each tool) buys us three things:

1. **One place to configure timeouts / retries / auth.** When a
   service-principal bearer token is configured (``token`` /
   ``OVID2_MCP_API_TOKEN``) it is attached to every request here; when
   absent the client stays network-perimeter only.
2. **A predictable error envelope.** :class:`WebApiError` carries the
   upstream HTTP status and body so tool handlers can turn it into an
   informative MCP ``CallToolResult`` with ``is_error=True``.
3. **In-process test double.** :func:`build_client_for_asgi` returns a
   client backed by ``httpx.ASGITransport`` so unit tests can drive
   the whole tool stack against an in-memory FastAPI app without
   spinning up uvicorn.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ovid2.mcp.settings import MCP_API_TOKEN, MCP_HTTP_TIMEOUT, MCP_UPSTREAM


class WebApiError(RuntimeError):
    """Raised when the upstream returns a non-2xx status code."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


class WebApiClient:
    """Async wrapper around :class:`httpx.AsyncClient`.

    The client is designed to be constructed once per server lifetime
    and reused across every tool call. Callers must ``await
    client.aclose()`` when they are done (the server lifespan handler
    takes care of that).
    """

    def __init__(
        self,
        base_url: str = MCP_UPSTREAM,
        *,
        timeout: float = MCP_HTTP_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = MCP_API_TOKEN,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers=headers,
        )

    @property
    def base_url(self) -> str:
        """Return the upstream base URL (no trailing slash)."""
        return self._base_url

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def get(self, path: str, /, **params: Any) -> Any:
        """Issue a ``GET`` and return the decoded JSON body.

        ``path`` is positional-only so a ``path=`` *query* parameter (the
        Data Space / filesystem tools) does not collide with it.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        response = await self._client.get(path, params=clean)
        return self._unwrap(response)

    async def get_text(self, path: str, /, **params: Any) -> str:
        """Issue a ``GET`` and return the raw ``text/plain`` body."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = await self._client.get(path, params=clean)
        if response.status_code >= 400:
            self._raise(response)
        return response.text

    async def post(self, path: str, /, json_body: Any = None, **params: Any) -> Any:
        """Issue a ``POST`` and return the decoded JSON body."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = await self._client.post(path, json=json_body, params=clean)
        return self._unwrap(response)

    async def put(self, path: str, json_body: Any = None) -> Any:
        """Issue a ``PUT`` and return the decoded JSON body."""
        response = await self._client.put(path, json=json_body)
        return self._unwrap(response)

    async def delete(self, path: str) -> Any:
        """Issue a ``DELETE`` and return the decoded JSON body."""
        response = await self._client.delete(path)
        return self._unwrap(response)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _unwrap(self, response: httpx.Response) -> Any:
        """Return the decoded JSON body, or raise :class:`WebApiError`."""
        if response.status_code >= 400:
            self._raise(response)
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        """Convert an HTTP error response into a :class:`WebApiError`."""
        body: Any
        try:
            body = response.json()
            message = (
                body.get("detail", response.reason_phrase) if isinstance(body, dict) else str(body)
            )
        except json.JSONDecodeError:
            body = response.text
            message = response.text or response.reason_phrase
        raise WebApiError(response.status_code, message, body=body)


def build_client_for_asgi(app: Any, base_url: str = "http://test") -> WebApiClient:
    """Return a :class:`WebApiClient` bound to an in-process ASGI app.

    Used by unit tests that want to exercise the tool code path
    end-to-end without a real uvicorn process. ``app`` should be a
    FastAPI instance (or any ASGI callable).
    """
    transport = httpx.ASGITransport(app=app)
    return WebApiClient(base_url=base_url, transport=transport)
