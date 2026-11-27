"""Rendered-page checks for full screen (``static/fullscreen.js``).

Renders real pages through the app and asserts the shell ships the
full-screen toggle and script on each one, that the static asset is
served, and that every response carries the same-origin framing headers
(the panel frames itself for full screen; nobody else may).
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/", "/navigator", "/manual", "/runs", "/pipelines"])
def test_fullscreen_toggle_on_every_page(web_client, path: str) -> None:  # type: ignore[no-untyped-def]
    resp = web_client.get(path)
    assert resp.status_code == 200, path
    body = resp.text
    assert 'id="fullscreen-toggle"' in body
    assert "data-fullscreen-toggle" in body
    assert "/static/fullscreen.js" in body
    assert "icon-maximize" in body and "icon-minimize" in body
    assert "focus-exit" not in body


@pytest.mark.parametrize("path", ["/", "/static/fullscreen.js", "/api/v1/health"])
def test_frame_ancestors_headers(web_client, path: str) -> None:  # type: ignore[no-untyped-def]
    resp = web_client.get(path)
    assert resp.headers.get("content-security-policy") == "frame-ancestors 'self'"
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"


def test_fullscreen_script_served(web_client) -> None:  # type: ignore[no-untyped-def]
    resp = web_client.get("/static/fullscreen.js")
    assert resp.status_code == 200
    assert "window.Ovid2Fullscreen" in resp.text


def test_frame_headers_do_not_override_route_headers() -> None:
    """A header a route sets itself is kept."""
    import asyncio

    from ovid2.web.frame_headers import FrameAncestorsMiddleware

    sent: list[dict] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"X-Frame-Options", b"DENY")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def send(msg):  # type: ignore[no-untyped-def]
        sent.append(msg)

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request"}

    asyncio.run(FrameAncestorsMiddleware(inner)({"type": "http"}, receive, send))
    headers = sent[0]["headers"]
    assert (b"X-Frame-Options", b"DENY") in headers
    assert (b"x-frame-options", b"SAMEORIGIN") not in headers
    assert (b"content-security-policy", b"frame-ancestors 'self'") in headers
