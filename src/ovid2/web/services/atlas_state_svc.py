"""Ovid Atlas collaboration state: shared saved views + presence / soft locks.

* **Saved views** (``web_atlas_layouts``) are shared by every member of the
  workspace: which layers are shown, which nodes are pinned, whether
  running lanes float to the top, the overlays and the dock size. Anyone with
  ``VIEW_PIPELINE_DATA`` reads them; saving / deleting needs
  ``MANAGE_CONFIG`` (enforced by the routes).
* **Presence** (``web_atlas_presence``) is a heartbeat per browser tab:
  ``(user, node, mode)`` where ``mode`` is ``view`` or ``edit``. Rows older
  than :data:`PRESENCE_TTL_S` are ignored and pruned. A row in ``edit``
  mode is a *soft lock*: other members see "X is editing" and must
  confirm before they edit the same node; nothing is ever blocked.

Every query filters / stamps ``workspace_id``
(``tests/contract/test_workspace_scoped_queries.py``). Display names are
the principal's e-mail (or id) — never a secret.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ovid2.web import db as web_db
from ovid2.workspaces import active_workspace

#: Seconds after which a presence heartbeat is stale.
PRESENCE_TTL_S: int = 45

#: Saved view names: short slugs.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_NODE_RE = re.compile(r"^[a-z_]+:[^\s]{1,300}$")

#: Top-level keys a saved view may carry (closed set — the browser applies
#: only these). ``expanded`` / ``mode`` / ``offsets`` / ``zoom`` are the
#: free-canvas camera of the pre-lane Atlas: still accepted so views saved
#: before the lane board load, but the browser ignores them. Pinned by
#: ``tests/unit/web/test_atlas.py``.
LAYOUT_KEYS: frozenset[str] = frozenset(
    {
        "layers",
        "pinned",
        "float_running",
        "dock",
        "overlays",
        "focus",
        "expanded",
        "mode",
        "offsets",
        "zoom",
    }
)
_MAX_LAYOUT_BYTES = 64_000


class AtlasStateError(ValueError):
    """Invalid saved view / presence payload."""


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_layout(layout: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(layout, dict):
        raise AtlasStateError("layout must be an object")
    unknown = set(layout) - LAYOUT_KEYS
    if unknown:
        raise AtlasStateError(f"unknown layout keys: {sorted(unknown)}")
    text = json.dumps(layout, separators=(",", ":"))
    if len(text) > _MAX_LAYOUT_BYTES:
        raise AtlasStateError("layout is too large")
    return layout


def list_layouts() -> list[dict[str, Any]]:
    """Return every saved view of the active workspace (newest first)."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT name, layout_json, created_by, updated_by, updated_at "
            "FROM web_atlas_layouts WHERE workspace_id = ? ORDER BY updated_at DESC",
            (active_workspace(),),
        ).fetchall()
    return [
        {
            "name": str(r["name"]),
            "layout": json.loads(str(r["layout_json"])),
            "created_by": str(r["created_by"]),
            "updated_by": str(r["updated_by"]),
            "updated_at": str(r["updated_at"]),
        }
        for r in rows
    ]


def save_layout(name: str, layout: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Create or replace the saved view ``name``.

    Raises:
        AtlasStateError: Bad name / layout.
    """
    if not _NAME_RE.match(name or ""):
        raise AtlasStateError("view names are 1-60 letters, digits, spaces, '.', '_' or '-'")
    body = json.dumps(_validate_layout(layout), separators=(",", ":"))
    now = _now().isoformat()
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_atlas_layouts "
            "(workspace_id, name, layout_json, created_by, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (workspace_id, name) DO UPDATE SET layout_json = excluded.layout_json, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (active_workspace(), name, body, actor, actor, now),
        )
    web_db.audit(actor=actor, action="atlas.layout.save", target=name)
    return {"name": name, "layout": layout, "updated_by": actor, "updated_at": now}


def delete_layout(name: str, *, actor: str) -> bool:
    """Delete the saved view ``name`` created by ``actor``.

    Owner-scoped: a member deletes only the views they created (others'
    views are shared work and stay).

    Returns:
        ``True`` when a view was deleted.

    Raises:
        PermissionError: The view exists but another member created it.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT created_by FROM web_atlas_layouts WHERE workspace_id = ? AND name = ?",
            (active_workspace(), name),
        ).fetchone()
        if row is None:
            return False
        if str(row["created_by"]) != actor:
            raise PermissionError(f"view '{name}' was created by another member")
        n = conn.execute(
            "DELETE FROM web_atlas_layouts WHERE workspace_id = ? AND name = ? AND created_by = ?",
            (active_workspace(), name, actor),
        ).rowcount
    if n:
        web_db.audit(actor=actor, action="atlas.layout.delete", target=name)
    return bool(n)


def heartbeat(
    *,
    user_id: str,
    display_name: str,
    client_id: str,
    node: str | None,
    mode: str,
) -> list[dict[str, Any]]:
    """Record one tab's presence and return everyone else's live presence.

    Args:
        user_id: Principal id.
        display_name: E-mail / id shown to the other members.
        client_id: Random per-tab id (one user may have several tabs).
        node: Atlas node id the tab is focused on (``None`` = none).
        mode: ``view`` or ``edit`` (an ``edit`` row is a soft lock).

    Raises:
        AtlasStateError: Malformed ids / mode.
    """
    if not _CLIENT_RE.match(client_id or ""):
        raise AtlasStateError("bad client id")
    if node is not None and not _NODE_RE.match(node):
        raise AtlasStateError("bad node id")
    if mode not in ("view", "edit"):
        raise AtlasStateError("mode must be view or edit")
    now = _now()
    stale = (now - timedelta(seconds=PRESENCE_TTL_S * 4)).isoformat()
    ws = active_workspace()
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_atlas_presence "
            "(workspace_id, user_id, client_id, display_name, node, mode, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (workspace_id, client_id) DO UPDATE SET user_id = excluded.user_id, "
            "display_name = excluded.display_name, node = excluded.node, mode = excluded.mode, "
            "seen_at = excluded.seen_at",
            (ws, user_id, client_id, display_name[:120], node, mode, now.isoformat()),
        )
        conn.execute(
            "DELETE FROM web_atlas_presence WHERE workspace_id = ? AND seen_at < ?",
            (ws, stale),
        )
    return presence(exclude_client=client_id)


def leave(client_id: str) -> None:
    """Drop one tab's presence row (page unload)."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "DELETE FROM web_atlas_presence WHERE workspace_id = ? AND client_id = ?",
            (active_workspace(), client_id),
        )


def presence(*, exclude_client: str | None = None) -> list[dict[str, Any]]:
    """Return the live presence rows of the active workspace."""
    cutoff = (_now() - timedelta(seconds=PRESENCE_TTL_S)).isoformat()
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id, client_id, display_name, node, mode, seen_at "
            "FROM web_atlas_presence WHERE workspace_id = ? AND seen_at >= ? "
            "ORDER BY seen_at DESC",
            (active_workspace(), cutoff),
        ).fetchall()
    return [
        {
            "user_id": str(r["user_id"]),
            "display_name": str(r["display_name"]),
            "node": r["node"],
            "mode": str(r["mode"]),
            "seen_at": str(r["seen_at"]),
        }
        for r in rows
        if r["client_id"] != exclude_client
    ]


def locks_for(node: str, *, exclude_user: str | None = None) -> list[dict[str, Any]]:
    """Return the live soft locks (``edit`` presence) on ``node``."""
    return [
        p
        for p in presence()
        if p["node"] == node and p["mode"] == "edit" and p["user_id"] != exclude_user
    ]


__all__ = [
    "LAYOUT_KEYS",
    "PRESENCE_TTL_S",
    "AtlasStateError",
    "delete_layout",
    "heartbeat",
    "leave",
    "list_layouts",
    "locks_for",
    "presence",
    "save_layout",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.web.services.atlas_state_svc is a library module.")
