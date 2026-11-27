"""HTML pages for workspaces (adr-workspaces.md).

- ``POST /workspaces/switch`` — the top-bar switcher (any signed-in user;
  only workspaces visible to the caller are accepted).
- ``GET /workspaces`` — list + create (system admins).
- ``GET /workspaces/{workspace_id}`` — members (system admins and that
  workspace's ``admin`` members): add / change role / remove; rename,
  archive / restore (system admins).

Every mutation carries the CSRF token and a capability gate
(``test_html_mutations_guarded.py``). Archive and delete need a human
approval from a *different* administrator (``workspace.archive`` /
``workspace.delete``), exactly like the JSON API.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ovid2 import workspaces
from ovid2.web.auth.csrf import require_csrf
from ovid2.web.auth.deps import WORKSPACE_COOKIE, AuthContext, require_capability
from ovid2.web.auth.roles import Capability, Role
from ovid2.web.services import approvals_svc, users_svc, workspaces_svc
from ovid2.web.templating import templates

router = APIRouter()


def _is_admin(ctx: AuthContext) -> bool:
    return ctx.role == Role.SYSTEM_ADMIN


def _safe_next(raw: str | None) -> str:
    """Return a same-site path to go back to (never an absolute / scheme URL)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


def _back(path: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    query = ""
    if error:
        query = "?error=" + quote(error)
    elif notice:
        query = "?notice=" + quote(notice)
    return RedirectResponse(url=path + query, status_code=303)


@router.post("/workspaces/switch", include_in_schema=False)
def workspace_switch_post(
    request: Request,
    workspace_id: str = Form(...),
    next_url: str = Form("/", alias="next"),
    ctx: AuthContext = Depends(require_capability(Capability.VIEW_PUBLIC)),
    _csrf: None = Depends(require_csrf),
) -> Response:
    """Switch the caller's session to ``workspace_id`` and go back.

    The target page is the *section* the user was on (``/pipelines/x`` →
    ``/pipelines``) because an item of the old workspace does not exist in
    the new one.
    """
    try:
        slug, _role = workspaces_svc.resolve_for(
            user_id=ctx.user_id,
            is_system_admin=_is_admin(ctx),
            requested=workspace_id,
        )
    except workspaces_svc.WorkspaceNotFound:
        raise HTTPException(status_code=404, detail="workspace_not_found") from None
    workspaces_svc.set_session_workspace(ctx.session_id, ctx.user_id, slug)
    target = _safe_next(next_url)
    section = "/" + target.lstrip("/").split("/", 1)[0].split("?", 1)[0]
    response = RedirectResponse(url=section if section != "/" else "/", status_code=303)
    if ctx.anonymous_bypass:
        # Sign-in is off: there is no session row, so remember the choice in
        # a (non-secret) cookie; it is re-validated on every request.
        response.set_cookie(
            WORKSPACE_COOKIE, slug, httponly=True, samesite="lax", path="/", max_age=86400 * 30
        )
    return response


@router.get(
    "/workspaces",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def workspaces_index(
    request: Request,
    error: str | None = None,
    notice: str | None = None,
    show_archived: bool = False,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
) -> HTMLResponse:
    """List every workspace with a create form (system admins)."""
    rows = workspaces_svc.list_workspaces(include_archived=True)
    archived = sum(1 for r in rows if r["status"] != "active")
    if not show_archived:
        rows = [r for r in rows if r["status"] == "active"]
    return templates.TemplateResponse(
        request,
        "workspaces.html",
        {
            "workspaces": rows,
            "archived_count": archived,
            "show_archived": show_archived,
            "error": error,
            "notice": notice,
            "active_workspace": ctx.workspace_id,
        },
    )


@router.post("/workspaces", include_in_schema=False)
def workspaces_create_post(
    request: Request,
    workspace_id: str = Form(...),
    display_name: str = Form(...),
    description: str = Form(""),
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Create a workspace and open its members page."""
    try:
        workspaces_svc.create_workspace(
            workspace_id.strip(),
            display_name,
            description=description,
            actor=ctx.email or ctx.user_id,
        )
    except workspaces_svc.WorkspaceError as exc:
        return _back("/workspaces", error=str(exc))
    return _back(f"/workspaces/{quote(workspace_id.strip())}", notice="Workspace created.")


def _detail_context(ctx: AuthContext, workspace_id: str) -> dict[str, Any]:
    record = workspaces_svc.get_workspace(workspace_id)
    if record is None:
        raise HTTPException(status_code=404)
    can_manage = workspaces_svc.can_manage_members(
        workspace_id, ctx.user_id, is_system_admin=_is_admin(ctx)
    )
    if not can_manage:
        # Members who are not workspace admins (and outsiders) get a 404,
        # so a workspace's member list is not disclosed.
        raise HTTPException(status_code=404)
    members = workspaces_svc.list_members(workspace_id)
    member_ids = {m["user_id"] for m in members}
    candidates = [
        u
        for u in users_svc.list_users()
        if u["user_id"] not in member_ids and u["status"] in ("active", "pending")
    ]
    candidates.sort(key=lambda u: str(u.get("email") or u["user_id"]).lower())
    return {
        "ws": record,
        "members": members,
        "candidates": candidates,
        "ws_roles": workspaces_svc.WS_ROLES,
        "is_system_admin": _is_admin(ctx),
        "is_default": workspace_id == workspaces.DEFAULT_WORKSPACE,
        "is_empty": workspaces_svc.workspace_is_empty(workspace_id),
    }


@router.get(
    "/workspaces/{workspace_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def workspace_detail(
    request: Request,
    workspace_id: str,
    error: str | None = None,
    notice: str | None = None,
    ctx: AuthContext = Depends(require_capability(Capability.VIEW_PUBLIC)),
) -> HTMLResponse:
    """Members page of one workspace (system admins / workspace admins)."""
    context = _detail_context(ctx, workspace_id)
    context.update({"error": error, "notice": notice})
    return templates.TemplateResponse(request, "workspace_detail.html", context)


def _require_manager(ctx: AuthContext, workspace_id: str) -> None:
    if workspaces_svc.get_workspace(workspace_id) is None or not workspaces_svc.can_manage_members(
        workspace_id, ctx.user_id, is_system_admin=_is_admin(ctx)
    ):
        raise HTTPException(status_code=404)


@router.post("/workspaces/{workspace_id}/members", include_in_schema=False)
def workspace_member_add_post(
    request: Request,
    workspace_id: str,
    user_id: str = Form(...),
    ws_role: str = Form("member"),
    ctx: AuthContext = Depends(require_capability(Capability.VIEW_PUBLIC)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Add a member (or change their workspace role)."""
    _require_manager(ctx, workspace_id)
    path = f"/workspaces/{quote(workspace_id)}"
    try:
        workspaces_svc.add_member(
            workspace_id, user_id, ws_role=ws_role, actor=ctx.email or ctx.user_id
        )
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back(path, notice="Member saved.")


@router.post("/workspaces/{workspace_id}/members/{user_id}/remove", include_in_schema=False)
def workspace_member_remove_post(
    request: Request,
    workspace_id: str,
    user_id: str,
    ctx: AuthContext = Depends(require_capability(Capability.VIEW_PUBLIC)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Remove a member."""
    _require_manager(ctx, workspace_id)
    path = f"/workspaces/{quote(workspace_id)}"
    try:
        workspaces_svc.remove_member(workspace_id, user_id, actor=ctx.email or ctx.user_id)
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back(path, notice="Member removed.")


@router.post("/workspaces/{workspace_id}/edit", include_in_schema=False)
def workspace_edit_post(
    request: Request,
    workspace_id: str,
    display_name: str = Form(...),
    description: str = Form(""),
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Rename / re-describe a workspace."""
    path = f"/workspaces/{quote(workspace_id)}"
    try:
        workspaces_svc.update_workspace(
            workspace_id,
            display_name=display_name,
            description=description,
            actor=ctx.email or ctx.user_id,
        )
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back(path, notice="Workspace updated.")


def _gated(action: str, workspace_id: str, ctx: AuthContext) -> str | None:
    """Return an approval notice when ``action`` still awaits approval, else ``None``."""
    try:
        approvals_svc.require_approval(action, workspace_id, requested_by=ctx.email or ctx.user_id)
    except approvals_svc.ApprovalRequired as exc:
        return (
            f"Approval {exc.approval_id} requested — another administrator must approve "
            f"'{action}' before it takes effect."
        )
    return None


@router.post("/workspaces/{workspace_id}/archive", include_in_schema=False)
def workspace_archive_post(
    request: Request,
    workspace_id: str,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Archive a workspace (after a human approval)."""
    path = f"/workspaces/{quote(workspace_id)}"
    if workspace_id == workspaces.DEFAULT_WORKSPACE:
        return _back(path, error="The default workspace cannot be archived.")
    pending = _gated("workspace.archive", workspace_id, ctx)
    if pending:
        return _back(path, notice=pending)
    try:
        workspaces_svc.archive_workspace(workspace_id, actor=ctx.email or ctx.user_id)
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back("/workspaces", notice=f"Workspace '{workspace_id}' archived.")


@router.post("/workspaces/{workspace_id}/restore", include_in_schema=False)
def workspace_restore_post(
    request: Request,
    workspace_id: str,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Restore an archived workspace."""
    path = f"/workspaces/{quote(workspace_id)}"
    try:
        workspaces_svc.restore_workspace(workspace_id, actor=ctx.email or ctx.user_id)
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back(path, notice="Workspace restored.")


@router.post("/workspaces/{workspace_id}/delete", include_in_schema=False)
def workspace_delete_post(
    request: Request,
    workspace_id: str,
    ctx: AuthContext = Depends(require_capability(Capability.MANAGE_WORKSPACES)),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """Delete an archived, empty workspace (after a human approval)."""
    path = f"/workspaces/{quote(workspace_id)}"
    record = workspaces_svc.get_workspace(workspace_id)
    if record is None:
        raise HTTPException(status_code=404)
    if (
        workspace_id == workspaces.DEFAULT_WORKSPACE
        or record["status"] != "archived"
        or not workspaces_svc.workspace_is_empty(workspace_id)
    ):
        return _back(path, error="Only an archived, empty workspace can be deleted.")
    pending = _gated("workspace.delete", workspace_id, ctx)
    if pending:
        return _back(path, notice=pending)
    try:
        workspaces_svc.delete_workspace(workspace_id, actor=ctx.email or ctx.user_id)
    except workspaces_svc.WorkspaceError as exc:
        return _back(path, error=str(exc))
    return _back("/workspaces", notice=f"Workspace '{workspace_id}' deleted.")


if __name__ == "__main__":
    raise SystemExit("workspaces_html is a library module; not runnable directly.")
