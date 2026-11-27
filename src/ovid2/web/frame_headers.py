"""Clickjacking protection: only the panel itself may frame its pages.

Every response carries ``Content-Security-Policy: frame-ancestors 'self'``
and ``X-Frame-Options: SAMEORIGIN``. The panel frames itself for full
screen (``static/fullscreen.js``); no other site may embed it. An
existing header set by a route is left alone.

Pure ASGI (no ``BaseHTTPMiddleware``) so streaming responses (SSE, the
LLM chat stream) are not buffered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Header values added to every HTTP response.
FRAME_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", b"frame-ancestors 'self'"),
    (b"x-frame-options", b"SAMEORIGIN"),
)


class FrameAncestorsMiddleware:
    """Add same-origin framing headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through, stamping the headers on ``http.response.start``."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for key, value in FRAME_HEADERS:
                    if key not in present:
                        headers.append((key, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
