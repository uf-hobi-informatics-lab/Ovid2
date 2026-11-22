"""Shared helper: enumerate every FastAPI ``APIRoute`` mounted on the app.

FastAPI ≥ 0.16x wraps each ``include_router`` in an ``_IncludedRouter``
whose own ``routes`` are hidden behind ``original_router``; walking
``app.routes`` alone sees only the wrappers. This walker descends through
both shapes and yields every concrete :class:`fastapi.routing.APIRoute`
together with the router-level dependencies inherited from its parents.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_api_routes(app: Any) -> Iterator[tuple[Any, list[Any]]]:
    """Yield ``(route, inherited_dependencies)`` for every APIRoute in ``app``.

    Args:
        app: A FastAPI application (or router).

    Yields:
        Each concrete ``APIRoute`` and the list of ``Depends`` objects
        declared on the router(s) / include contexts that contain it.
    """
    from fastapi.routing import APIRoute

    def _walk(routes: list[Any], inherited: list[Any]) -> Iterator[tuple[Any, list[Any]]]:
        for route in routes:
            if isinstance(route, APIRoute):
                yield route, inherited
                continue
            original = getattr(route, "original_router", None)
            if original is not None:
                ctx = getattr(route, "include_context", None)
                extra = list(getattr(ctx, "dependencies", None) or [])
                extra += list(getattr(original, "dependencies", None) or [])
                yield from _walk(list(original.routes), inherited + extra)

    yield from _walk(list(app.routes), [])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the contract tests.")
