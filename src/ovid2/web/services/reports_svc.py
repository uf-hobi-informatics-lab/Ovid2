"""Report Studio library service (web tier): reports, versions, ACL, exports.

A *report* is a saved layout (``report_layout.v1.json``) bound to one
report source (:mod:`report_sources_svc`). This service owns:

- **library** — create (blank / from a built-in or user template /
  duplicate), list with scopes (mine, shared with me, favourites, all
  visible), search, folders, tags, archive / restore;
- **versions** — every save snapshots the layout into
  ``web_report_versions``; optimistic concurrency via ``expected_version``;
  restore re-saves an old snapshot as a new version;
- **edit ops** — the closed :data:`ovid2.reports.layout.OPS` vocabulary
  shared with the editor and the assistant's ``edit_report_layout`` UI
  action;
- **sharing** — private by default; the owner grants ``view`` / ``edit`` to
  a user id or a role. Every grant / revoke is audited (``web_audit``);
- **templates** — users can save a layout as a reusable template, private
  or shared with everyone;
- **exports** — writes a private bundle and runs
  ``ovid2 tools report-export`` through the scoped executor broker, so
  reportlab / python-docx / vl-convert never load in the web process.

Principals: a human is their ``user_id``. The assistant's per-turn token
(``llm:<user>``) acts *as* that user for reads. Role shares match the
caller's role. With auth disabled every caller is ``anonymous``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.config import paths as xdg_paths
from ovid2.reports import catalogue
from ovid2.reports import layout as layout_mod
from ovid2.reports.compose import render_model
from ovid2.reports.formats import EXPORT_FORMATS
from ovid2.web import db as web_db
from ovid2.web.services import report_sources_svc
from ovid2.web.services._broker import BrokerError, SecretScope, run_tool_json

logger = logging.getLogger(__name__)

__all__ = [
    "EXPORT_TTL_SECONDS",
    "Principal",
    "ReportConflict",
    "ReportError",
    "ReportForbidden",
    "ReportNotFound",
    "apply_ops",
    "archive_report",
    "create_report",
    "delete_user_template",
    "duplicate_report",
    "export_report",
    "get_export",
    "get_report",
    "get_version",
    "list_folders",
    "list_reports",
    "list_shares",
    "list_templates",
    "list_versions",
    "preview_model",
    "redact_source",
    "render_report",
    "restore_report",
    "restore_version",
    "revoke_share",
    "save_as_template",
    "set_favorite",
    "set_share",
    "update_report",
]

#: Downloadable export files are removed after this many seconds.
EXPORT_TTL_SECONDS = 3600

#: Wall-clock cap for one export subprocess (vl-convert + reportlab).
EXPORT_TIMEOUT_SECONDS = 180.0

_MAX_TITLE = 160
_MAX_FOLDER = 120
_MAX_TAGS = 12
_MAX_TAG = 40
_MAX_VERSIONS_KEPT = 200
_PERMS = ("view", "edit")
_PRINCIPAL_KINDS = ("user", "role")


class ReportError(ValueError):
    """Raised for an invalid request (bad layout, bad field)."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


class ReportNotFound(LookupError):
    """Raised when a report / version / template id is unknown or invisible."""


class ReportForbidden(PermissionError):
    """Raised when the caller may see the report but not perform the action."""


class ReportConflict(RuntimeError):
    """Raised when ``expected_version`` does not match the stored version."""

    def __init__(self, current: int) -> None:
        super().__init__(f"report changed since you opened it (now version {current})")
        self.current = current


@dataclass(frozen=True)
class Principal:
    """The caller, reduced to what the ACL needs.

    Attributes:
        user_id: Human user id (``llm:<user>`` is mapped to ``<user>``).
        role: Role string (``viewer``, ``pipeline_operator`` …).
        agent: ``True`` for assistant / MCP principals.
        actor: Audit label (the raw principal id).
    """

    user_id: str
    role: str
    agent: bool = False
    actor: str = ""

    @classmethod
    def of(cls, user_id: str, role: str) -> Principal:
        """Build a principal from an auth context's ``user_id`` / role value."""
        agent = user_id.startswith(("llm:", "mcp:"))
        uid = user_id[len("llm:") :] if user_id.startswith("llm:") else user_id
        return cls(user_id=uid, role=role, agent=agent, actor=user_id)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return "rpt_" + secrets.token_hex(6)


# ---------------------------------------------------------------------------
# Field hygiene
# ---------------------------------------------------------------------------


def _clean_title(value: Any, default: str = "Untitled report") -> str:
    text = " ".join(str(value or "").split())[:_MAX_TITLE]
    return text or default


def _clean_folder(value: Any) -> str:
    parts = [p.strip() for p in str(value or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))[:_MAX_FOLDER]


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportError("tags must be a list of strings")
    out: list[str] = []
    for tag in value:
        t = " ".join(str(tag).split()).lower()[:_MAX_TAG]
        if t and t not in out:
            out.append(t)
    return out[:_MAX_TAGS]


def _validated(layout: Any, source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(layout, dict):
        raise ReportError("layout must be an object")
    norm = layout_mod.normalise(layout)
    errs = layout_mod.validate_layout(norm, source)
    if errs:
        raise ReportError("layout invalid", errs)
    return norm


# ---------------------------------------------------------------------------
# Row mapping + ACL
# ---------------------------------------------------------------------------


def _shares_for(conn: Any, report_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT principal_kind, principal, permission, granted_by, granted_at "
        "FROM web_report_shares WHERE report_id = ? ORDER BY principal_kind, principal",
        (report_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _permission(row: Any, shares: list[dict[str, Any]], who: Principal) -> str | None:
    """Return ``owner`` / ``edit`` / ``view`` / ``None`` for ``who``."""
    if row["owner"] == who.user_id:
        return "owner"
    best: str | None = None
    for s in shares:
        hit = (s["principal_kind"] == "user" and s["principal"] == who.user_id) or (
            s["principal_kind"] == "role" and s["principal"] == who.role
        )
        if hit:
            if s["permission"] == "edit":
                return "edit"
            best = "view"
    return best


def _summary(row: Any, perm: str, favorite: bool, shares: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except ValueError:
        tags = []
    layout = json.loads(row["layout_json"])
    blocks = sum(len(s.get("blocks") or []) for s in layout.get("sections") or [])
    return {
        "report_id": row["report_id"],
        "title": row["title"],
        "description": row["description"],
        "owner": row["owner"],
        "folder": row["folder"],
        "tags": tags,
        "source_id": row["source_id"],
        "template_id": row["template_id"],
        "theme": layout.get("theme") or catalogue.DEFAULT_THEME,
        "sections": len(layout.get("sections") or []),
        "blocks": blocks,
        "version": int(row["version"]),
        "status": row["status"],
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "permission": perm,
        "favorite": favorite,
        "shared": bool(shares),
    }


def _load(conn: Any, report_id: str, who: Principal) -> tuple[Any, list[dict[str, Any]], str]:
    row = conn.execute("SELECT * FROM web_reports WHERE report_id = ?", (report_id,)).fetchone()
    if row is None:
        raise ReportNotFound(report_id)
    shares = _shares_for(conn, report_id)
    perm = _permission(row, shares, who)
    if perm is None:
        # Indistinguishable from "missing" so ids cannot be probed.
        raise ReportNotFound(report_id)
    return row, shares, perm


def _is_favorite(conn: Any, report_id: str, who: Principal) -> bool:
    hit = conn.execute(
        "SELECT 1 FROM web_report_favorites WHERE user_id = ? AND report_id = ?",
        (who.user_id, report_id),
    ).fetchone()
    return hit is not None


def _require_edit(perm: str) -> None:
    if perm not in ("owner", "edit"):
        raise ReportForbidden("you have view-only access to this report")


def _require_owner(perm: str) -> None:
    if perm != "owner":
        raise ReportForbidden("only the owner can do this")


def _source_or_error(source_id: str) -> dict[str, Any]:
    try:
        return report_sources_svc.get_source(source_id)
    except report_sources_svc.SourceNotFound as exc:
        raise ReportError(f"unknown report source {source_id!r}") from exc


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


def _template_layout(template_id: str, who: Principal) -> tuple[dict[str, Any], str]:
    builtin = catalogue.get_template(template_id)
    if builtin is not None:
        return dict(builtin.layout), builtin.source_kind
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_report_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
    if row is None or (row["owner"] != who.user_id and not row["shared"]):
        raise ReportError(f"unknown template {template_id!r}")
    return json.loads(row["layout_json"]), str(row["source_kind"])


def create_report(
    who: Principal,
    *,
    source_id: str,
    template_id: str | None = None,
    layout: dict[str, Any] | None = None,
    title: str | None = None,
    description: str = "",
    folder: str = "",
    tags: list[str] | None = None,
    status: str = "saved",
) -> dict[str, Any]:
    """Create a report from a template, an explicit layout, or blank.

    With neither ``template_id`` nor ``layout``, the source's first
    recommended template is used (or an empty layout).
    """
    web_db.ensure_schema()
    source = _source_or_error(source_id)
    if layout is None:
        tid = template_id or next(iter(source.get("recommended_templates") or []), None)
        if tid:
            layout, kind = _template_layout(tid, who)
            if kind != source["kind"]:
                raise ReportError(
                    f"template {tid!r} is for {kind!r} sources, not {source['kind']!r}"
                )
            template_id = tid
        else:
            layout = layout_mod.empty_layout(source.get("title") or "Untitled report")
    layout = dict(layout)
    if title:
        layout["title"] = _clean_title(title)
    norm = _validated(layout, source)
    rid = _new_id()
    now = _now()
    final_title = _clean_title(norm.get("title"))
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_reports (report_id, owner, title, description, folder, tags_json, source_id, "
            "template_id, layout_json, version, status, created_at, updated_at, updated_by, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0)",
            (
                rid,
                who.user_id,
                final_title,
                str(description or "")[:2000],
                _clean_folder(folder),
                json.dumps(_clean_tags(tags)),
                source_id,
                template_id,
                json.dumps(norm),
                status if status in ("saved", "draft") else "saved",
                now,
                now,
                who.actor or who.user_id,
            ),
        )
        conn.execute(
            "INSERT INTO web_report_versions (report_id, version, layout_json, saved_at, saved_by, note) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (rid, json.dumps(norm), now, who.actor or who.user_id, "created"),
        )
    web_db.audit(
        actor=who.actor or who.user_id,
        action="report.create",
        target=rid,
        detail_json=json.dumps({"source_id": source_id, "template_id": template_id}),
    )
    return get_report(rid, who)


def duplicate_report(report_id: str, who: Principal, *, title: str | None = None) -> dict[str, Any]:
    """Copy a visible report into a new one owned by ``who``."""
    current = get_report(report_id, who)
    return create_report(
        who,
        source_id=current["source_id"],
        layout=current["layout"],
        title=title or f"{current['title']} (copy)",
        description=current["description"],
        folder=current["folder"] if current["permission"] == "owner" else "",
        tags=current["tags"],
    )


def get_report(report_id: str, who: Principal) -> dict[str, Any]:
    """Return one report (summary + layout + shares when owner)."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row, shares, perm = _load(conn, report_id, who)
        fav = _is_favorite(conn, report_id, who)
    out = _summary(row, perm, fav, shares)
    out["layout"] = json.loads(row["layout_json"])
    out["shares"] = shares if perm == "owner" else []
    return out


def list_reports(
    who: Principal,
    *,
    scope: str = "all",
    q: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    source_id: str | None = None,
    archived: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """Return the reports visible to ``who``.

    Args:
        scope: ``all`` (everything visible), ``mine``, ``shared`` (shared
            with me), ``favorites``.
        q: Case-insensitive substring on title / description / tags.
        folder: Exact folder (``""`` = unfiled); ``None`` = any.
        tag: Exact tag.
        source_id: Only reports on this source.
        archived: List archived reports instead of live ones.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM web_reports WHERE archived = ? ORDER BY updated_at DESC",
            (1 if archived else 0,),
        ).fetchall()
        favs = {
            r[0]
            for r in conn.execute(
                "SELECT report_id FROM web_report_favorites WHERE user_id = ?", (who.user_id,)
            ).fetchall()
        }
        out: list[dict[str, Any]] = []
        needle = (q or "").strip().lower()
        for row in rows:
            shares = _shares_for(conn, row["report_id"])
            perm = _permission(row, shares, who)
            if perm is None:
                continue
            if scope == "mine" and perm != "owner":
                continue
            if scope == "shared" and perm == "owner":
                continue
            if scope == "favorites" and row["report_id"] not in favs:
                continue
            if folder is not None and row["folder"] != folder:
                continue
            if source_id and row["source_id"] != source_id:
                continue
            summary = _summary(row, perm, row["report_id"] in favs, shares)
            if tag and tag.lower() not in summary["tags"]:
                continue
            if needle:
                hay = " ".join([summary["title"], summary["description"], *summary["tags"]]).lower()
                if needle not in hay:
                    continue
            out.append(summary)
    return {"total": len(out), "reports": out[: max(1, min(limit, 1000))]}


def list_folders(who: Principal) -> list[dict[str, Any]]:
    """Return ``[{folder, count}]`` over the caller's *own* live reports."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT folder, COUNT(*) AS n FROM web_reports WHERE owner = ? AND archived = 0 "
            "GROUP BY folder ORDER BY folder",
            (who.user_id,),
        ).fetchall()
    return [{"folder": r["folder"], "count": int(r["n"])} for r in rows]


def update_report(
    report_id: str,
    who: Principal,
    *,
    layout: dict[str, Any] | None = None,
    title: str | None = None,
    description: str | None = None,
    folder: str | None = None,
    tags: list[str] | None = None,
    expected_version: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Save changes; a layout change bumps the version and snapshots it.

    Folder and tags are the owner's organisation and need ownership;
    layout / title / description need edit permission.
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        row, _shares, perm = _load(conn, report_id, who)
    _require_edit(perm)
    if (folder is not None or tags is not None) and perm != "owner":
        raise ReportForbidden("only the owner can file or tag this report")
    current = int(row["version"])
    if expected_version is not None and int(expected_version) != current:
        raise ReportConflict(current)
    new_layout = json.loads(row["layout_json"])
    changed_layout = False
    if layout is not None:
        new_layout = _validated(layout, _source_or_error(row["source_id"]))
        changed_layout = True
    if title is not None:
        new_layout["title"] = _clean_title(title)
        changed_layout = True
    version = current + 1 if changed_layout else current
    now = _now()
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_reports SET title = ?, description = ?, folder = ?, tags_json = ?, layout_json = ?, "
            "version = ?, status = 'saved', updated_at = ?, updated_by = ? WHERE report_id = ? AND version = ?",
            (
                _clean_title(new_layout.get("title")),
                str(description if description is not None else row["description"])[:2000],
                _clean_folder(folder) if folder is not None else row["folder"],
                json.dumps(_clean_tags(tags)) if tags is not None else row["tags_json"],
                json.dumps(new_layout),
                version,
                now,
                who.actor or who.user_id,
                report_id,
                current,
            ),
        )
        if changed_layout:
            conn.execute(
                "INSERT INTO web_report_versions (report_id, version, layout_json, saved_at, saved_by, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    version,
                    json.dumps(new_layout),
                    now,
                    who.actor or who.user_id,
                    str(note)[:200],
                ),
            )
            conn.execute(
                "DELETE FROM web_report_versions WHERE report_id = ? AND version <= ?",
                (report_id, version - _MAX_VERSIONS_KEPT),
            )
    return get_report(report_id, who)


def apply_ops(
    report_id: str,
    who: Principal,
    ops: list[dict[str, Any]],
    *,
    expected_version: int | None = None,
    save: bool = True,
    base_layout: dict[str, Any] | None = None,
    render: bool = False,
    redact: bool = False,
) -> dict[str, Any]:
    """Apply edit ops atomically; save (new version) unless ``save=False``.

    Args:
        base_layout: Apply to this (unsaved, browser working-copy) layout
            instead of the stored one. Only with ``save=False``.
        render: With ``save=False``, also return the render model.

    Returns:
        ``{report, notes}`` when saved, else ``{layout, notes[, model]}``.
    """
    current = get_report(report_id, who)
    _require_edit(current["permission"])
    if base_layout is not None and save:
        raise ReportError("a base layout can only be used with save=false")
    source = _source_or_error(current["source_id"])
    try:
        new_layout, notes = layout_mod.apply_ops(
            base_layout if base_layout is not None else current["layout"], ops, source
        )
    except layout_mod.LayoutError as exc:
        raise ReportError("edit rejected", exc.errors) from exc
    if not save:
        out: dict[str, Any] = {"layout": new_layout, "notes": notes}
        if render:
            src = redact_source(source) if redact else source
            out["model"] = render_model(new_layout, src, width="container", meta=_meta(current))
        return out
    report = update_report(
        report_id,
        who,
        layout=new_layout,
        expected_version=expected_version,
        note="; ".join(notes)[:200],
    )
    return {"report": report, "notes": notes}


def archive_report(report_id: str, who: Principal) -> dict[str, Any]:
    """Archive (soft-delete) a report; owner only; reversible."""
    with web_db.connect() as conn:
        _row, _s, perm = _load(conn, report_id, who)
        _require_owner(perm)
        conn.execute(
            "UPDATE web_reports SET archived = 1, updated_at = ? WHERE report_id = ?",
            (_now(), report_id),
        )
    web_db.audit(actor=who.actor or who.user_id, action="report.archive", target=report_id)
    return {"ok": True, "report_id": report_id, "archived": True}


def restore_report(report_id: str, who: Principal) -> dict[str, Any]:
    """Un-archive a report; owner only."""
    with web_db.connect() as conn:
        _row, _s, perm = _load(conn, report_id, who)
        _require_owner(perm)
        conn.execute(
            "UPDATE web_reports SET archived = 0, updated_at = ? WHERE report_id = ?",
            (_now(), report_id),
        )
    web_db.audit(actor=who.actor or who.user_id, action="report.restore", target=report_id)
    return get_report(report_id, who)


def set_favorite(report_id: str, who: Principal, favorite: bool) -> dict[str, Any]:
    """Star / un-star a visible report for the caller."""
    with web_db.connect() as conn:
        _load(conn, report_id, who)
        if favorite:
            conn.execute(
                "INSERT OR IGNORE INTO web_report_favorites (user_id, report_id, created_at) VALUES (?, ?, ?)",
                (who.user_id, report_id, _now()),
            )
        else:
            conn.execute(
                "DELETE FROM web_report_favorites WHERE user_id = ? AND report_id = ?",
                (who.user_id, report_id),
            )
    return {"ok": True, "report_id": report_id, "favorite": favorite}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def list_versions(report_id: str, who: Principal) -> list[dict[str, Any]]:
    """Return the version history, newest first (no layouts)."""
    with web_db.connect() as conn:
        _load(conn, report_id, who)
        rows = conn.execute(
            "SELECT version, saved_at, saved_by, note FROM web_report_versions WHERE report_id = ? "
            "ORDER BY version DESC",
            (report_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_version(report_id: str, version: int, who: Principal) -> dict[str, Any]:
    """Return one snapshot with its layout."""
    with web_db.connect() as conn:
        _load(conn, report_id, who)
        row = conn.execute(
            "SELECT * FROM web_report_versions WHERE report_id = ? AND version = ?",
            (report_id, int(version)),
        ).fetchone()
    if row is None:
        raise ReportNotFound(f"{report_id}@{version}")
    out = dict(row)
    out["layout"] = json.loads(out.pop("layout_json"))
    return out


def restore_version(report_id: str, version: int, who: Principal) -> dict[str, Any]:
    """Re-save an old snapshot as the newest version."""
    snap = get_version(report_id, version, who)
    return update_report(report_id, who, layout=snap["layout"], note=f"restored version {version}")


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


def list_shares(report_id: str, who: Principal) -> list[dict[str, Any]]:
    """Return the ACL (owner only)."""
    with web_db.connect() as conn:
        _row, shares, perm = _load(conn, report_id, who)
    _require_owner(perm)
    return shares


def set_share(
    report_id: str, who: Principal, *, principal_kind: str, principal: str, permission: str
) -> list[dict[str, Any]]:
    """Grant (or change) ``view`` / ``edit`` for a user or role; owner only; audited."""
    if who.agent:
        raise ReportForbidden("sharing is a human-only action")
    if principal_kind not in _PRINCIPAL_KINDS:
        raise ReportError("principal_kind must be user or role")
    if permission not in _PERMS:
        raise ReportError("permission must be view or edit")
    principal = str(principal or "").strip()
    if not principal or len(principal) > 200:
        raise ReportError("principal is required")
    if principal_kind == "role":
        from ovid2.web.auth.roles import Role

        if principal not in {r.value for r in Role} or principal == Role.PENDING.value:
            raise ReportError(f"unknown role {principal!r}")
    elif principal == who.user_id:
        raise ReportError("you already own this report")
    with web_db.connect() as conn:
        _row, _s, perm = _load(conn, report_id, who)
        _require_owner(perm)
        conn.execute(
            "INSERT INTO web_report_shares (report_id, principal_kind, principal, permission, granted_by, granted_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(report_id, principal_kind, principal) DO UPDATE SET "
            "permission = excluded.permission, granted_by = excluded.granted_by, granted_at = excluded.granted_at",
            (report_id, principal_kind, principal, permission, who.user_id, _now()),
        )
    web_db.audit(
        actor=who.actor or who.user_id,
        action="report.share",
        target=report_id,
        detail_json=json.dumps(
            {"principal_kind": principal_kind, "principal": principal, "permission": permission}
        ),
    )
    return list_shares(report_id, who)


def revoke_share(
    report_id: str, who: Principal, *, principal_kind: str, principal: str
) -> list[dict[str, Any]]:
    """Remove a grant; owner only; audited."""
    if who.agent:
        raise ReportForbidden("sharing is a human-only action")
    with web_db.connect() as conn:
        _row, _s, perm = _load(conn, report_id, who)
        _require_owner(perm)
        conn.execute(
            "DELETE FROM web_report_shares WHERE report_id = ? AND principal_kind = ? AND principal = ?",
            (report_id, principal_kind, principal),
        )
    web_db.audit(
        actor=who.actor or who.user_id,
        action="report.unshare",
        target=report_id,
        detail_json=json.dumps({"principal_kind": principal_kind, "principal": principal}),
    )
    return list_shares(report_id, who)


# ---------------------------------------------------------------------------
# Templates (built-in + user)
# ---------------------------------------------------------------------------


def list_templates(who: Principal, source_kind: str | None = None) -> list[dict[str, Any]]:
    """Return built-in templates plus the caller's own and shared user templates."""
    web_db.ensure_schema()
    out = [t.as_dict() for t in catalogue.list_templates(source_kind)]
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM web_report_templates WHERE (owner = ? OR shared = 1) "
            "AND (? IS NULL OR source_kind = ?) ORDER BY name",
            (who.user_id, source_kind, source_kind),
        ).fetchall()
    for r in rows:
        out.append(
            {
                "template_id": r["template_id"],
                "source_kind": r["source_kind"],
                "name": r["name"],
                "description": r["description"],
                "builtin": False,
                "owner": r["owner"],
                "shared": bool(r["shared"]),
            }
        )
    return out


def get_template(template_id: str, who: Principal) -> dict[str, Any]:
    """Return one template with its layout."""
    builtin = catalogue.get_template(template_id)
    if builtin is not None:
        return builtin.as_dict(with_layout=True)
    try:
        layout, kind = _template_layout(template_id, who)
    except ReportError as exc:
        raise ReportNotFound(template_id) from exc
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_report_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
    return {
        "template_id": template_id,
        "source_kind": kind,
        "name": row["name"],
        "description": row["description"],
        "builtin": False,
        "owner": row["owner"],
        "shared": bool(row["shared"]),
        "layout": layout,
    }


def save_as_template(
    report_id: str, who: Principal, *, name: str, description: str = "", shared: bool = False
) -> dict[str, Any]:
    """Save a visible report's layout as a user template."""
    if who.agent:
        raise ReportForbidden("saving templates is a human-only action")
    report = get_report(report_id, who)
    source = _source_or_error(report["source_id"])
    tid = "user." + secrets.token_hex(5)
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_report_templates (template_id, owner, name, description, source_kind, layout_json, "
            "shared, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid,
                who.user_id,
                _clean_title(name, "My template"),
                str(description or "")[:2000],
                source["kind"],
                json.dumps(report["layout"]),
                1 if shared else 0,
                _now(),
            ),
        )
    web_db.audit(
        actor=who.actor or who.user_id,
        action="report.template_save",
        target=tid,
        detail_json=json.dumps({"report_id": report_id, "shared": bool(shared)}),
    )
    return get_template(tid, who)


def delete_user_template(template_id: str, who: Principal) -> dict[str, Any]:
    """Delete a user template the caller owns."""
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT owner FROM web_report_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
        if row is None:
            raise ReportNotFound(template_id)
        if row["owner"] != who.user_id:
            raise ReportForbidden("only the owner can delete this template")
        conn.execute("DELETE FROM web_report_templates WHERE template_id = ?", (template_id,))
    web_db.audit(
        actor=who.actor or who.user_id, action="report.template_delete", target=template_id
    )
    return {"ok": True, "template_id": template_id}


# ---------------------------------------------------------------------------
# Rendering + export
# ---------------------------------------------------------------------------


def _meta(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report["report_id"],
        "report_version": report["version"],
        "owner": report["owner"],
        "updated_at": report["updated_at"],
        "rendered_at": _now(),
    }


def source_has_row_values(source: dict[str, Any]) -> bool:
    """Return ``True`` when any dataset column is tagged ``row_values``."""
    for ds in (source.get("datasets") or {}).values():
        for c in ds.get("columns") or []:
            if c.get("sensitivity") == "row_values":
                return True
    return False


#: Placeholder written over ``row_values`` cells for callers who may not
#: see pipeline data values.
REDACTED = "<redacted>"


def redact_source(source: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``source`` with every ``row_values`` cell masked.

    Aggregates (counts, rates, check outcomes) are untouched; only columns
    tagged ``sensitivity: row_values`` — raw source values such as the
    analysis ``top_values`` — are replaced by :data:`REDACTED`.
    """
    out = dict(source)
    datasets: dict[str, Any] = {}
    for name, ds in (source.get("datasets") or {}).items():
        hidden = [
            c["name"] for c in ds.get("columns") or [] if c.get("sensitivity") == "row_values"
        ]
        if not hidden:
            datasets[name] = ds
            continue
        rows = [
            {**r, **{h: REDACTED for h in hidden if r.get(h) is not None}}
            for r in ds.get("rows") or []
        ]
        datasets[name] = {**ds, "rows": rows}
    out["datasets"] = datasets
    return out


def _render_source(source_id: str, redact: bool) -> dict[str, Any]:
    source = _source_or_error(source_id)
    return redact_source(source) if redact else source


def render_report(report_id: str, who: Principal, *, redact: bool = False) -> dict[str, Any]:
    """Return the browser render model for the saved layout."""
    report = get_report(report_id, who)
    source = _render_source(report["source_id"], redact)
    return render_model(report["layout"], source, width="container", meta=_meta(report))


def preview_model(
    report_id: str, who: Principal, layout: dict[str, Any], *, redact: bool = False
) -> dict[str, Any]:
    """Validate an unsaved layout and return its render model."""
    report = get_report(report_id, who)
    source = _render_source(report["source_id"], redact)
    norm = _validated(layout, source)
    return render_model(norm, source, width="container", meta=_meta(report))


def export_dir() -> Path:
    """Return the private export area (mode 0700)."""
    override = os.environ.get("OVID2_REPORT_EXPORT_DIR")
    base = Path(override) if override else xdg_paths.state_dir() / "report_exports"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def prune_exports(max_age: float = EXPORT_TTL_SECONDS) -> int:
    """Remove export folders older than ``max_age`` seconds."""
    cutoff = time.time() - max_age
    removed = 0
    for entry in export_dir().iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


@dataclass(frozen=True)
class ExportFile:
    """A rendered export ready to stream."""

    export_id: str
    path: Path
    filename: str
    media_type: str
    size: int


_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def get_export(export_id: str, who: Principal) -> ExportFile:
    """Return a finished export the caller produced (404 for anyone else)."""
    if not _EXPORT_ID_RE.match(export_id or ""):
        raise ReportNotFound(export_id)
    meta_path = export_dir() / export_id / "export.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportNotFound(export_id) from exc
    if meta.get("user_id") != who.user_id:
        raise ReportNotFound(export_id)
    path = Path(meta["path"])
    if not path.is_file():
        raise ReportNotFound(export_id)
    return ExportFile(export_id, path, meta["filename"], meta["media_type"], int(meta["size"]))


def export_report(
    report_id: str,
    who: Principal,
    fmt: str,
    *,
    layout: dict[str, Any] | None = None,
    block_id: str | None = None,
    redact: bool = False,
) -> ExportFile:
    """Render the report (or an unsaved ``layout``) to ``fmt`` via the executor.

    Hidden sections / blocks are never exported (the render model drops
    them). Every export is audited.
    """
    if fmt not in EXPORT_FORMATS:
        raise ReportError(f"unknown export format {fmt!r}")
    report = get_report(report_id, who)
    source = _render_source(report["source_id"], redact)
    final_layout = _validated(layout, source) if layout is not None else report["layout"]
    prune_exports()
    work = export_dir() / secrets.token_hex(12)
    work.mkdir(mode=0o700)
    bundle_path = work / "bundle.json"
    fd = os.open(bundle_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {"layout": final_layout, "source": source, "meta": _meta(report)}, fh, default=str
        )
    out_dir = work / "out"
    out_dir.mkdir(mode=0o700)
    args = [
        "report-export",
        "--bundle",
        str(bundle_path),
        "--format",
        fmt,
        "--out-dir",
        str(out_dir),
    ]
    if block_id:
        args += ["--block", block_id]
    try:
        result = run_tool_json(args, scope=SecretScope(), timeout=EXPORT_TIMEOUT_SECONDS)
    except BrokerError as exc:
        logger.warning("report export %s/%s failed: %s", report_id, fmt, exc)
        raise ReportError(f"export failed: {exc}") from exc
    finally:
        bundle_path.unlink(missing_ok=True)
    if not result.get("ok"):
        raise ReportError(f"export failed: {result.get('message') or 'unknown error'}")
    path = Path(str(result["path"])).resolve()
    if out_dir.resolve() not in path.parents or not path.is_file():
        raise ReportError("export produced no file")
    web_db.audit(
        actor=who.actor or who.user_id,
        action="report.export",
        target=report_id,
        detail_json=json.dumps(
            {"format": fmt, "version": report["version"], "block": block_id, "redacted": redact}
        ),
    )
    out = ExportFile(
        export_id=work.name,
        path=path,
        filename=str(result["filename"]),
        media_type=str(result["media_type"]),
        size=int(result.get("size") or path.stat().st_size),
    )
    fd = os.open(work / "export.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "user_id": who.user_id,
                "report_id": report_id,
                "path": str(path),
                "filename": out.filename,
                "media_type": out.media_type,
                "size": out.size,
            },
            fh,
        )
    return out


def export_formats() -> list[dict[str, str]]:
    """Return the export format catalogue."""
    return [
        {"format": k, "extension": v[0], "media_type": v[1], "label": v[2]}
        for k, v in EXPORT_FORMATS.items()
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the ovid2 web routes.")
