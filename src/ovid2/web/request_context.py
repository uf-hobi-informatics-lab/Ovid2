"""Per-request principal attribution (adr-workspaces.md).

Historically every human action was recorded as the literal actor ``web``,
so members of a shared workspace could not tell who submitted a job or
changed a config. The workspace middleware binds the request principal's
label here; :func:`resolve_actor` swaps the generic ``web`` label for it
when an audit row or a job row is written. Agent labels (``mcp:*`` /
``llm:*``), ``worker`` and ``workflow:*`` are left untouched.
"""

from __future__ import annotations

import contextvars

__all__ = ["bind_actor", "reset_actor", "resolve_actor"]

_GENERIC: frozenset[str] = frozenset({"web", "api"})

_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ovid2_request_actor", default=None
)


def bind_actor(label: str | None) -> contextvars.Token[str | None]:
    """Bind the request principal's audit label; return a reset token."""
    return _ACTOR.set(label or None)


def reset_actor(token: contextvars.Token[str | None]) -> None:
    """Undo :func:`bind_actor`."""
    _ACTOR.reset(token)


def resolve_actor(actor: str) -> str:
    """Return the real principal label when ``actor`` is the generic ``web``."""
    if actor in _GENERIC:
        bound = _ACTOR.get()
        if bound:
            return bound
    return actor


if __name__ == "__main__":
    raise SystemExit("request_context is a library module; not runnable directly.")
