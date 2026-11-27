"""Saved Agent Ovid chat sessions (server-side conversation store).

Each chat with the right-panel assistant is a *session*: an ordered list
of user / assistant messages plus a title and token counters, owned by
one principal id. While ``settings.auth.enabled`` is false every caller
is the synthetic ``anonymous`` principal, so the deployment shares one
history.

Storage is the web-tier SQLite DB (``web_llm_sessions`` /
``web_llm_messages``). Message text may contain PHI, so it lives only
here — never in ``web_audit`` (audit rows stay metadata-only) and never
in logs. Users delete their own sessions; nothing expires automatically.

Keyword search uses SQLite FTS5 when the build provides it (a shadow
``web_llm_messages_fts`` table maintained by this module), and falls back
to a parameterised ``LIKE`` scan otherwise.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ovid2.web import db as web_db
from ovid2.workspaces import active_workspace

__all__ = [
    "MAX_TITLE_CHARS",
    "SessionNotFoundError",
    "append_turn",
    "create_session",
    "delete_session",
    "fallback_title",
    "get_session",
    "history_for_turn",
    "list_sessions",
    "rename_session",
    "set_generated_title",
    "set_provisional_title",
    "update_message_meta",
    "valid_id",
]

MAX_TITLE_CHARS = 80
MAX_META_CHARS = 64_000
_ID_RE = re.compile(r"^[a-f0-9]{16,32}$")
_FTS_READY: dict[str, bool] = {}


class SessionNotFoundError(LookupError):
    """Raised when a session does not exist or belongs to someone else."""


@dataclass
class SessionSummary:
    """One row of the history list."""

    session_id: str
    title: str
    title_source: str
    created_at: str
    updated_at: str
    message_count: int = 0
    last_prompt_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    model: str | None = None
    snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return dict(self.__dict__)


@dataclass
class SessionDetail(SessionSummary):
    """A session with its messages."""

    messages: list[dict[str, Any]] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure() -> bool:
    """Ensure the schema (and FTS shadow table) exist; return FTS availability."""
    from ovid2.web.settings import web_db_path

    key = str(web_db_path())
    if key in _FTS_READY:
        return _FTS_READY[key]
    web_db.ensure_schema()
    fts = True
    with web_db.connect() as conn:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS web_llm_messages_fts "
                "USING fts5(content, session_id UNINDEXED, message_id UNINDEXED)"
            )
        except sqlite3.OperationalError:
            fts = False
    _FTS_READY[key] = fts
    return fts


def valid_id(session_id: str) -> bool:
    """Return True when ``session_id`` has the shape this module mints."""
    return bool(_ID_RE.match(session_id or ""))


def fallback_title(text: str) -> str:
    """Derive a title from the first prompt (first line, ~60 chars)."""
    line = " ".join(str(text or "").split())
    if not line:
        return "New session"
    if len(line) <= 60:
        return line
    cut = line[:60].rsplit(" ", 1)[0] or line[:60]
    return cut.rstrip(" ,.;:") + "…"


def _clean_title(title: str) -> str:
    t = " ".join(str(title or "").split()).strip().strip("\"'`*#").strip()
    t = re.sub(r"^(title\s*:\s*)", "", t, flags=re.IGNORECASE)
    return t[:MAX_TITLE_CHARS]


def _owned(conn: sqlite3.Connection, user_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM web_llm_sessions WHERE session_id = ? AND user_id = ? AND workspace_id = ?",
        (session_id, user_id, active_workspace()),
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(session_id)
    result: sqlite3.Row = row
    return result


def _summary(row: sqlite3.Row, count: int = 0, snippet: str | None = None) -> SessionSummary:
    return SessionSummary(
        session_id=row["session_id"],
        title=row["title"] or "New session",
        title_source=row["title_source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=count,
        last_prompt_tokens=int(row["last_prompt_tokens"] or 0),
        total_prompt_tokens=int(row["total_prompt_tokens"] or 0),
        total_completion_tokens=int(row["total_completion_tokens"] or 0),
        model=row["model"],
        snippet=snippet,
    )


def create_session(user_id: str, title: str = "") -> SessionSummary:
    """Create an empty session for ``user_id`` and return it."""
    _ensure()
    sid = secrets.token_hex(12)
    now = _now()
    clean = _clean_title(title)
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_llm_sessions "
            "(session_id, user_id, title, title_source, created_at, updated_at, workspace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, user_id, clean, "user" if clean else "pending", now, now, active_workspace()),
        )
        row = _owned(conn, user_id, sid)
    return _summary(row)


def _snippet(text: str, query: str, width: int = 120) -> str:
    flat = " ".join(text.split())
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    pos = -1
    for term in terms:
        pos = flat.lower().find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, pos - width // 3)
    out = flat[start : start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def _fts_query(query: str) -> str:
    # Quote every term so FTS operators in user input are inert; prefix match.
    terms = [t.replace('"', "") for t in re.split(r"\s+", query.strip()) if t.replace('"', "")]
    return " ".join(f'"{t}"*' for t in terms)


def list_sessions(
    user_id: str, *, query: str | None = None, limit: int = 50, offset: int = 0
) -> list[SessionSummary]:
    """Return ``user_id``'s sessions, newest first, optionally keyword-filtered.

    A query matches session titles and message text (every term must
    appear; prefix match). Each hit carries a short snippet around the
    first match.
    """
    fts = _ensure()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    q = (query or "").strip()[:200]
    with web_db.connect() as conn:
        counts = {
            r["session_id"]: int(r["n"])
            for r in conn.execute(
                "SELECT m.session_id, COUNT(*) AS n FROM web_llm_messages m "
                "JOIN web_llm_sessions s ON s.session_id = m.session_id "
                "WHERE s.user_id = ? AND s.workspace_id = ? GROUP BY m.session_id",
                (user_id, active_workspace()),
            )
        }
        if not q:
            rows = conn.execute(
                "SELECT * FROM web_llm_sessions WHERE user_id = ? AND workspace_id = ? "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (user_id, active_workspace(), limit, offset),
            ).fetchall()
            return [_summary(r, counts.get(r["session_id"], 0)) for r in rows]

        terms = [t for t in re.split(r"\s+", q) if t]
        sessions = conn.execute(
            "SELECT * FROM web_llm_sessions WHERE user_id = ? AND workspace_id = ? "
            "ORDER BY updated_at DESC",
            (user_id, active_workspace()),
        ).fetchall()
        # session_id -> snippet source text ("" = matched on title only)
        hits: dict[str, str] = {}
        for r in sessions:
            if _contains_all(r["title"] or "", terms):
                hits[r["session_id"]] = ""
        if fts:
            msg_rows = conn.execute(
                "SELECT f.session_id AS session_id, f.content AS content "
                "FROM web_llm_messages_fts f "
                "JOIN web_llm_sessions s ON s.session_id = f.session_id "
                "WHERE s.user_id = ? AND web_llm_messages_fts MATCH ? "
                "ORDER BY f.rowid DESC LIMIT 2000",
                (user_id, _fts_query(q)),
            ).fetchall()
        else:
            msg_rows = conn.execute(
                "SELECT m.session_id AS session_id, m.content AS content "
                "FROM web_llm_messages m JOIN web_llm_sessions s "
                "ON s.session_id = m.session_id WHERE s.user_id = ? "
                "AND m.content LIKE ? ESCAPE '\\' ORDER BY m.message_id DESC LIMIT 2000",
                (user_id, f"%{_escape_like(terms[0])}%"),
            ).fetchall()
        for r in msg_rows:
            if not fts and not _contains_all(r["content"], terms):
                continue
            if not hits.get(r["session_id"]):
                hits[r["session_id"]] = r["content"]
    matched = [r for r in sessions if r["session_id"] in hits][offset : offset + limit]
    return [
        _summary(
            r,
            counts.get(r["session_id"], 0),
            _snippet(hits[r["session_id"]], q) if hits[r["session_id"]] else None,
        )
        for r in matched
    ]


def _contains_all(text: str, terms: list[str]) -> bool:
    low = (text or "").lower()
    return all(t.lower() in low for t in terms)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_session(user_id: str, session_id: str) -> SessionDetail:
    """Return one session with all its messages.

    Raises:
        SessionNotFoundError: When it does not exist or is not ``user_id``'s.
    """
    _ensure()
    with web_db.connect() as conn:
        row = _owned(conn, user_id, session_id)
        msgs = conn.execute(
            "SELECT seq, role, content, meta_json, created_at FROM web_llm_messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for m in msgs:
        item: dict[str, Any] = {"seq": int(m["seq"]), "role": m["role"], "content": m["content"]}
        if m["meta_json"]:
            try:
                item["meta"] = json.loads(m["meta_json"])
            except json.JSONDecodeError:
                item["meta"] = {}
        out.append(item)
    base = _summary(row, len(out))
    return SessionDetail(**base.__dict__, messages=out)


def history_for_turn(user_id: str, session_id: str, max_turns: int = 40) -> list[dict[str, str]]:
    """Return the trailing ``{role, content}`` window the model sees next turn."""
    detail = get_session(user_id, session_id)
    msgs = [
        {"role": m["role"], "content": str(m["content"])[:32000]}
        for m in detail.messages
        if m["role"] in ("user", "assistant") and str(m["content"]).strip()
    ]
    return msgs[-max_turns:]


def _bounded_meta(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    raw = json.dumps(meta, ensure_ascii=False, default=str)
    return raw if len(raw) <= MAX_META_CHARS else None


def append_turn(
    user_id: str,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    context_tokens: int | None = None,
    connection: str | None = None,
    model: str | None = None,
) -> SessionSummary:
    """Append ``messages`` (``{role, content, meta?}``) and update counters.

    Args:
        user_id: Owner.
        session_id: Target session.
        messages: The turn's messages in order.
        prompt_tokens: Prompt tokens billed this turn (summed over tool steps).
        completion_tokens: Completion tokens billed this turn.
        context_tokens: Size of the largest prompt sent this turn — the
            current context occupancy; ``None`` leaves it unchanged.
        connection: Connection that answered.
        model: Model that answered.

    Raises:
        SessionNotFoundError: When the session is missing or foreign.
    """
    fts = _ensure()
    now = _now()
    with web_db.connect() as conn:
        _owned(conn, user_id, session_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM web_llm_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["s"])
            for m in messages:
                role = str(m.get("role"))
                if role not in ("user", "assistant"):
                    continue
                seq += 1
                content = str(m.get("content") or "")[:64000]
                cur = conn.execute(
                    "INSERT INTO web_llm_messages "
                    "(session_id, seq, role, content, meta_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, seq, role, content, _bounded_meta(m.get("meta")), now),
                )
                if fts and content.strip():
                    conn.execute(
                        "INSERT INTO web_llm_messages_fts (content, session_id, message_id) "
                        "VALUES (?, ?, ?)",
                        (content, session_id, cur.lastrowid),
                    )
            conn.execute(
                "UPDATE web_llm_sessions SET updated_at = ?, "
                "total_prompt_tokens = total_prompt_tokens + ?, "
                "total_completion_tokens = total_completion_tokens + ?, "
                "last_prompt_tokens = COALESCE(?, last_prompt_tokens), "
                "connection = COALESCE(?, connection), model = COALESCE(?, model) "
                "WHERE session_id = ?",
                (
                    now,
                    int(prompt_tokens),
                    int(completion_tokens),
                    None if context_tokens is None else int(context_tokens),
                    connection,
                    model,
                    session_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row = _owned(conn, user_id, session_id)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM web_llm_messages WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
    return _summary(row, int(count))


def set_generated_title(session_id: str, title: str, source: str) -> bool:
    """Set a title only while the session is still ``pending`` a title.

    Used by the background titler; never overwrites a user's rename.

    Returns:
        True when the title was written.
    """
    _ensure()
    clean = _clean_title(title)
    if not clean:
        return False
    with web_db.connect() as conn:
        cur = conn.execute(
            "UPDATE web_llm_sessions SET title = ?, title_source = ? "
            "WHERE session_id = ? AND title_source = 'pending'",
            (clean, source, session_id),
        )
        return cur.rowcount > 0


def set_provisional_title(session_id: str, title: str) -> None:
    """Show ``title`` while the generated one is pending (keeps ``pending``)."""
    _ensure()
    clean = _clean_title(title)
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_llm_sessions SET title = ? WHERE session_id = ? "
            "AND title_source = 'pending' AND title = ''",
            (clean, session_id),
        )


def update_message_meta(user_id: str, session_id: str, seq: int, meta: dict[str, Any]) -> None:
    """Replace the display metadata of assistant message ``seq``.

    The browser calls this after the user runs / dismisses a proposal or
    undoes a UI action, so a reopened session shows the final state.

    Raises:
        SessionNotFoundError: When the session or message does not exist.
        ValueError: When the metadata is too large.
    """
    _ensure()
    raw = _bounded_meta(meta)
    if meta and raw is None:
        raise ValueError("Message metadata is too large.")
    with web_db.connect() as conn:
        _owned(conn, user_id, session_id)
        cur = conn.execute(
            "UPDATE web_llm_messages SET meta_json = ? "
            "WHERE session_id = ? AND seq = ? AND role = 'assistant'",
            (raw, session_id, int(seq)),
        )
        if cur.rowcount == 0:
            raise SessionNotFoundError(f"{session_id}#{seq}")


def rename_session(user_id: str, session_id: str, title: str) -> SessionSummary:
    """Rename a session (marks the title as user-chosen).

    Raises:
        SessionNotFoundError: When the session is missing or foreign.
        ValueError: When the title is empty.
    """
    _ensure()
    clean = _clean_title(title)
    if not clean:
        raise ValueError("Title must not be empty.")
    with web_db.connect() as conn:
        _owned(conn, user_id, session_id)
        conn.execute(
            "UPDATE web_llm_sessions SET title = ?, title_source = 'user' WHERE session_id = ?",
            (clean, session_id),
        )
        row = _owned(conn, user_id, session_id)
    return _summary(row)


def delete_session(user_id: str, session_id: str) -> None:
    """Delete a session and all its messages.

    Raises:
        SessionNotFoundError: When the session is missing or foreign.
    """
    fts = _ensure()
    with web_db.connect() as conn:
        _owned(conn, user_id, session_id)
        conn.execute("DELETE FROM web_llm_messages WHERE session_id = ?", (session_id,))
        if fts:
            conn.execute("DELETE FROM web_llm_messages_fts WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM web_llm_sessions WHERE session_id = ?", (session_id,))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web services.")
