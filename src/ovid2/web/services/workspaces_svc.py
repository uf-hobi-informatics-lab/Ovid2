"""Workspace + membership store (adr-workspaces.md).

A workspace is the tenant boundary for saved pipelines, workflows, jobs,
runs, reports, connections, addresses, secrets, engines, LLM connections,
the transform library and workspace pipeline packages. This module owns:

* the ``web_workspaces`` / ``web_workspace_members`` tables (CRUD),
* the on-disk tree of a non-default workspace (created on
  :func:`create_workspace`),
* **resolution** — which workspace a principal works in for one request
  (:func:`resolve_for`), and who may see / manage a workspace.

Authorization model (industry-standard "global role ceiling + workspace
membership"): a user's global role decides *what* they may do; membership
decides *where*. ``system_admin`` sees every workspace; a membership with
``ws_role='admin'`` may manage that workspace's members only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ovid2 import workspaces as ws_paths
from ovid2.web import db as web_db

logger = logging.getLogger(__name__)

__all__ = [
    "WS_ROLES",
    "MembershipView",
    "WorkspaceError",
    "WorkspaceNotFound",
    "add_member",
    "archive_workspace",
    "can_manage_members",
    "create_workspace",
    "delete_workspace",
    "enrol_default",
    "get_workspace",
    "is_member",
    "list_members",
    "list_workspaces",
    "member_role",
    "remember_last",
    "remove_member",
    "resolve_for",
    "restore_workspace",
    "set_session_workspace",
    "update_workspace",
    "visible_workspaces",
    "workspace_is_empty",
]

#: Workspace-level roles. ``admin`` = may manage that workspace's members.
WS_ROLES: tuple[str, ...] = ("member", "admin")

_STATUS_ACTIVE = "active"
_STATUS_ARCHIVED = "archived"

#: Resource sub-directories created inside a new workspace's tree.
_KIND_DIRS: tuple[str, ...] = (
    "connections",
    "configs",
    "engines",
    "llms",
    "transforms",
    "pipelines",
    "workflows",
)


class WorkspaceError(ValueError):
    """Raised when a workspace operation is invalid."""


class WorkspaceNotFound(WorkspaceError):
    """Raised when a workspace (or a membership) does not exist."""


@dataclass(frozen=True)
class MembershipView:
    """One workspace as seen by one principal.

    Attributes:
        workspace_id: Slug.
        display_name: Human label.
        ws_role: ``member`` / ``admin``, or ``None`` when the principal sees
            it only because they are a system admin.
        status: ``active`` / ``archived``.
    """

    workspace_id: str
    display_name: str
    ws_role: str | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict."""
        return {
            "workspace_id": self.workspace_id,
            "display_name": self.display_name,
            "ws_role": self.ws_role,
            "status": self.status,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ws_row(row: sqlite3.Row, member_count: int | None = None) -> dict[str, Any]:
    out = {
        "workspace_id": row["workspace_id"],
        "display_name": row["display_name"],
        "description": row["description"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if member_count is not None:
        out["member_count"] = member_count
    return out


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


def list_workspaces(*, include_archived: bool = False) -> list[dict[str, Any]]:
    """Return every workspace (``default`` first), with member counts."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM web_workspace_members m "
            "WHERE m.workspace_id = w.workspace_id) AS n FROM web_workspaces w "
            "ORDER BY CASE WHEN w.workspace_id = 'default' THEN 0 ELSE 1 END, "
            "lower(w.display_name)"
        ).fetchall()
    return [
        _ws_row(r, int(r["n"])) for r in rows if include_archived or r["status"] == _STATUS_ACTIVE
    ]


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    """Return one workspace record, or ``None``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM web_workspace_members m "
            "WHERE m.workspace_id = w.workspace_id) AS n FROM web_workspaces w "
            "WHERE w.workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    return _ws_row(row, int(row["n"])) if row is not None else None


def _require(workspace_id: str) -> dict[str, Any]:
    record = get_workspace(workspace_id)
    if record is None:
        raise WorkspaceNotFound(f"Workspace '{workspace_id}' not found.")
    return record


def _materialise_tree(slug: str) -> None:
    """Create the on-disk tree of a non-default workspace (0o755 / secrets 0o700)."""
    if slug == ws_paths.DEFAULT_WORKSPACE:
        return
    ws_paths.materialise(slug, _KIND_DIRS)


def create_workspace(
    slug: str,
    display_name: str,
    *,
    description: str = "",
    actor: str,
) -> dict[str, Any]:
    """Create a workspace and its on-disk tree.

    Raises:
        WorkspaceError: Bad slug / name, or the slug is taken.
    """
    try:
        ws_paths.validate_slug(slug)
    except ws_paths.WorkspaceSlugError as exc:
        raise WorkspaceError(str(exc)) from None
    name = (display_name or "").strip()
    if not name or len(name) > 80:
        raise WorkspaceError("Workspace name is required (max 80 characters).")
    web_db.ensure_schema()
    now = _now()
    with web_db.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO web_workspaces (workspace_id, display_name, description, status, "
                "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, name, (description or "").strip()[:500], _STATUS_ACTIVE, actor, now, now),
            )
        except sqlite3.IntegrityError:
            raise WorkspaceError(f"Workspace '{slug}' already exists.") from None
    _materialise_tree(slug)
    with ws_paths.use_workspace(slug):
        web_db.audit(
            actor=actor,
            action="workspace.create",
            target=slug,
            detail_json=json.dumps({"display_name": name}),
        )
    return _require(slug)


def update_workspace(
    workspace_id: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
    actor: str,
) -> dict[str, Any]:
    """Rename / re-describe a workspace (the slug never changes)."""
    current = _require(workspace_id)
    name = current["display_name"] if display_name is None else display_name.strip()
    if not name or len(name) > 80:
        raise WorkspaceError("Workspace name is required (max 80 characters).")
    desc = current["description"] if description is None else description.strip()[:500]
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_workspaces SET display_name = ?, description = ?, updated_at = ? "
            "WHERE workspace_id = ?",
            (name, desc, _now(), workspace_id),
        )
    with ws_paths.use_workspace(workspace_id):
        web_db.audit(actor=actor, action="workspace.update", target=workspace_id)
    return _require(workspace_id)


def _set_status(workspace_id: str, status: str, *, actor: str, action: str) -> dict[str, Any]:
    if workspace_id == ws_paths.DEFAULT_WORKSPACE and status != _STATUS_ACTIVE:
        raise WorkspaceError("The default workspace cannot be archived.")
    _require(workspace_id)
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_workspaces SET status = ?, updated_at = ? WHERE workspace_id = ?",
            (status, _now(), workspace_id),
        )
        if status != _STATUS_ACTIVE:
            # Sessions pinned to an archived workspace fall back on next request.
            conn.execute(
                "UPDATE web_sessions SET active_workspace_id = NULL WHERE active_workspace_id = ?",
                (workspace_id,),
            )
    with ws_paths.use_workspace(workspace_id):
        web_db.audit(actor=actor, action=action, target=workspace_id)
    return _require(workspace_id)


def archive_workspace(workspace_id: str, *, actor: str) -> dict[str, Any]:
    """Archive a workspace: members can no longer switch into it."""
    return _set_status(workspace_id, _STATUS_ARCHIVED, actor=actor, action="workspace.archive")


def restore_workspace(workspace_id: str, *, actor: str) -> dict[str, Any]:
    """Re-activate an archived workspace."""
    return _set_status(workspace_id, _STATUS_ACTIVE, actor=actor, action="workspace.restore")


def workspace_is_empty(workspace_id: str) -> bool:
    """Return ``True`` when no job / report / config file belongs to the workspace."""
    with web_db.connect() as conn:
        for table in ("web_jobs", "web_workflow_runs", "web_reports"):
            hit = conn.execute(
                "SELECT 1 FROM " + table + " WHERE workspace_id = ? LIMIT 1", (workspace_id,)
            ).fetchone()
            if hit is not None:
                return False
    root = ws_paths.config_root(workspace_id)
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file():
                return False
    return True


def delete_workspace(workspace_id: str, *, actor: str) -> None:
    """Delete an *archived, empty* workspace (rows + memberships + empty tree).

    Raises:
        WorkspaceError: ``default``, still active, or not empty.
    """
    record = _require(workspace_id)
    if workspace_id == ws_paths.DEFAULT_WORKSPACE:
        raise WorkspaceError("The default workspace cannot be deleted.")
    if record["status"] != _STATUS_ARCHIVED:
        raise WorkspaceError("Archive the workspace before deleting it.")
    if not workspace_is_empty(workspace_id):
        raise WorkspaceError("The workspace still holds jobs, reports or configuration files.")
    with web_db.connect() as conn:
        conn.execute("DELETE FROM web_workspace_members WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM web_workspaces WHERE workspace_id = ?", (workspace_id,))
    root = ws_paths.config_root(workspace_id)
    if root.is_dir():
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
        root.rmdir()
    web_db.audit(actor=actor, action="workspace.delete", target=workspace_id)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def list_members(workspace_id: str) -> list[dict[str, Any]]:
    """Return the members of a workspace joined with their user record."""
    _require(workspace_id)
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT m.user_id, m.ws_role, m.added_by, m.added_at, u.email, u.name, u.role, "
            "u.status FROM web_workspace_members m LEFT JOIN web_users u "
            "ON u.user_id = m.user_id WHERE m.workspace_id = ? "
            "ORDER BY lower(coalesce(u.email, m.user_id))",
            (workspace_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def member_role(workspace_id: str, user_id: str) -> str | None:
    """Return the principal's ``ws_role`` in a workspace, or ``None``."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT ws_role FROM web_workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
    return str(row["ws_role"]) if row is not None else None


def is_member(workspace_id: str, user_id: str) -> bool:
    """Return ``True`` when ``user_id`` belongs to ``workspace_id``."""
    return member_role(workspace_id, user_id) is not None


def add_member(
    workspace_id: str, user_id: str, *, ws_role: str = "member", actor: str
) -> dict[str, Any]:
    """Add (or re-role) a member. The user must exist and not be disabled."""
    if ws_role not in WS_ROLES:
        raise WorkspaceError(f"Workspace role must be one of {', '.join(WS_ROLES)}.")
    record = _require(workspace_id)
    if record["status"] != _STATUS_ACTIVE:
        raise WorkspaceError("Members cannot be added to an archived workspace.")
    with web_db.connect() as conn:
        user = conn.execute("SELECT status FROM web_users WHERE user_id = ?", (user_id,)).fetchone()
        if user is None:
            raise WorkspaceNotFound(f"User '{user_id}' not found.")
        if user["status"] == "disabled":
            raise WorkspaceError("A disabled user cannot be added to a workspace.")
        conn.execute(
            "INSERT INTO web_workspace_members (workspace_id, user_id, ws_role, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(workspace_id, user_id) DO UPDATE SET "
            "ws_role = excluded.ws_role",
            (workspace_id, user_id, ws_role, actor, _now()),
        )
    with ws_paths.use_workspace(workspace_id):
        web_db.audit(
            actor=actor,
            action="workspace.member_add",
            target=user_id,
            detail_json=json.dumps({"ws_role": ws_role}),
        )
    return {"workspace_id": workspace_id, "user_id": user_id, "ws_role": ws_role}


def remove_member(workspace_id: str, user_id: str, *, actor: str) -> None:
    """Remove a membership; the user's sessions pinned to it fall back."""
    _require(workspace_id)
    with web_db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM web_workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        if cur.rowcount == 0:
            raise WorkspaceNotFound(f"User '{user_id}' is not a member of '{workspace_id}'.")
        conn.execute(
            "UPDATE web_sessions SET active_workspace_id = NULL "
            "WHERE user_id = ? AND active_workspace_id = ?",
            (user_id, workspace_id),
        )
    with ws_paths.use_workspace(workspace_id):
        web_db.audit(actor=actor, action="workspace.member_remove", target=user_id)


def enrol_default(user_id: str, *, actor: str) -> None:
    """Enrol a newly activated user in ``default`` when they have no workspace."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        has_any = conn.execute(
            "SELECT 1 FROM web_workspace_members WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if has_any is not None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO web_workspace_members "
            "(workspace_id, user_id, ws_role, added_by, added_at) VALUES (?, ?, 'member', ?, ?)",
            (ws_paths.DEFAULT_WORKSPACE, user_id, actor, _now()),
        )


def user_workspace_ids(user_id: str) -> list[str]:
    """Return the slugs of the *active* workspaces ``user_id`` belongs to."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT m.workspace_id FROM web_workspace_members m JOIN web_workspaces w "
            "ON w.workspace_id = m.workspace_id WHERE m.user_id = ? AND w.status = ?",
            (user_id, _STATUS_ACTIVE),
        ).fetchall()
    return sorted(str(r["workspace_id"]) for r in rows)


def visible_workspaces(user_id: str, *, is_system_admin: bool) -> list[MembershipView]:
    """Return the active workspaces a principal may switch into.

    A system admin sees every active workspace (``ws_role`` is their real
    membership role, or ``None``); everyone else sees their memberships.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT w.workspace_id, w.display_name, w.status, m.ws_role FROM web_workspaces w "
            "LEFT JOIN web_workspace_members m ON m.workspace_id = w.workspace_id "
            "AND m.user_id = ? WHERE w.status = ? "
            "ORDER BY CASE WHEN w.workspace_id = 'default' THEN 0 ELSE 1 END, "
            "lower(w.display_name)",
            (user_id, _STATUS_ACTIVE),
        ).fetchall()
    return [
        MembershipView(
            workspace_id=str(r["workspace_id"]),
            display_name=str(r["display_name"]),
            ws_role=(str(r["ws_role"]) if r["ws_role"] else None),
            status=str(r["status"]),
        )
        for r in rows
        if is_system_admin or r["ws_role"]
    ]


def can_manage_members(workspace_id: str, user_id: str, *, is_system_admin: bool) -> bool:
    """System admins, or that workspace's ``admin`` members."""
    if is_system_admin:
        return True
    return member_role(workspace_id, user_id) == "admin"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _session_workspace(session_id: str | None) -> str | None:
    if not session_id:
        return None
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT active_workspace_id FROM web_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None or not row["active_workspace_id"]:
        return None
    return str(row["active_workspace_id"])


def _last_workspace(user_id: str) -> str | None:
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT last_workspace_id FROM web_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None or not row["last_workspace_id"]:
        return None
    return str(row["last_workspace_id"])


def resolve_for(
    *,
    user_id: str,
    is_system_admin: bool,
    requested: str | None = None,
    session_id: str | None = None,
    allowed: tuple[str, ...] | None = None,
) -> tuple[str, str | None]:
    """Resolve the workspace a principal works in for one request.

    Order: explicit ``requested`` (``X-Ovid2-Workspace`` header) → the
    session's active workspace → the user's last-used workspace →
    ``default`` → the first visible workspace.

    Args:
        user_id: Principal id (a ``web_users`` id, or a synthetic one).
        is_system_admin: Whether the principal sees every workspace.
        requested: Explicitly requested slug, if any.
        session_id: The browser session, if any.
        allowed: For non-user principals (MCP tokens): the closed set of
            slugs the principal may use. ``None`` → membership-based.

    Returns:
        ``(workspace_id, ws_role)``. ``workspace_id`` is
        :data:`ovid2.workspaces.NO_WORKSPACE` when the principal belongs to
        no workspace.

    Raises:
        WorkspaceNotFound: ``requested`` is not visible to the principal
            (deliberately indistinguishable from "does not exist").
    """
    web_db.ensure_schema()
    if allowed is not None:
        active = {w["workspace_id"] for w in list_workspaces()}
        visible = [w for w in allowed if w in active]
        roles: dict[str, str | None] = dict.fromkeys(visible)
    else:
        views = visible_workspaces(user_id, is_system_admin=is_system_admin)
        visible = [v.workspace_id for v in views]
        roles = {v.workspace_id: v.ws_role for v in views}
    if requested:
        if requested not in roles:
            raise WorkspaceNotFound(f"Workspace '{requested}' not found.")
        return requested, roles[requested]
    for candidate in (
        _session_workspace(session_id),
        _last_workspace(user_id) if allowed is None else None,
        ws_paths.DEFAULT_WORKSPACE,
    ):
        if candidate and candidate in roles:
            return candidate, roles[candidate]
    if visible:
        return visible[0], roles[visible[0]]
    return ws_paths.NO_WORKSPACE, None


def set_session_workspace(session_id: str | None, user_id: str, workspace_id: str) -> None:
    """Pin a browser session to ``workspace_id`` and remember it for the user."""
    with web_db.connect() as conn:
        if session_id:
            conn.execute(
                "UPDATE web_sessions SET active_workspace_id = ? WHERE session_id = ?",
                (workspace_id, session_id),
            )
    remember_last(user_id, workspace_id)


def remember_last(user_id: str, workspace_id: str) -> None:
    """Store ``workspace_id`` as the user's last-used workspace (no-op for synthetic ids)."""
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_users SET last_workspace_id = ? WHERE user_id = ?",
            (workspace_id, user_id),
        )


if __name__ == "__main__":
    raise SystemExit("workspaces_svc is a library module; not runnable directly.")
