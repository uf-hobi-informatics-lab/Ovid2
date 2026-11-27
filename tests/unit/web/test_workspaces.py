"""Workspaces end-to-end through the real HTTP app (adr-workspaces.md).

Real users + signed session cookies, auth on. Covers:

* migration: existing data / users land in ``default``;
* admin creates a workspace, adds members; members switch;
* members of a workspace share connections, secrets, jobs and reports;
  outsiders get 404 (never 403) — even by id or with the header;
* each member keeps their global role inside the workspace;
* a workspace admin can manage members but not create workspaces;
* archive / delete pass the human-approval gate;
* the assistant token is pinned to the user's workspace; MCP tokens are
  bound to listed workspaces.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from ovid2 import workspaces
from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import (
    approvals_svc,
    jobs_svc,
    sessions_svc,
    settings_svc,
    users_svc,
    workspaces_svc,
)


@pytest.fixture(autouse=True)
def _env(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture points every *_DIR override at the flat tree; keep them —
    # they apply only to ``default`` (non-default workspaces ignore them).
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()
    settings = GlobalSettings(auth=SettingsAuth(enabled=True))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


@pytest.fixture
def client() -> Iterator[TestClient]:
    c = TestClient(create_app())
    try:
        yield c
    finally:
        c.close()


def _user(role: Role, subject: str) -> tuple[str, str]:
    rec = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    rec = users_svc.activate(user_id=rec["user_id"], role=role, actor="test")
    sess = sessions_svc.create(
        user_id=str(rec["user_id"]), idp_sid=None, absolute_ttl_min=60, idle_ttl_min=45
    )
    resp = Response()
    cookie_mod.issue_cookie(resp, sess.session_id)
    line = next(v.decode("latin-1") for k, v in resp.raw_headers if k == b"set-cookie")
    jar: SimpleCookie[str] = SimpleCookie()
    jar.load(line)
    return str(rec["user_id"]), jar["ovid2_session"].value


def _as(client: TestClient, cookie: str) -> TestClient:
    client.cookies.clear()
    client.cookies.set("ovid2_session", cookie)
    return client


def _csrf(client: TestClient) -> str:
    """Return the CSRF token rendered into an authenticated page."""
    html = client.get("/").text
    marker = '"X-CSRF-Token": "'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


@pytest.fixture
def team(client: TestClient) -> dict[str, tuple[str, str]]:
    """admin, alice (operator), bob (viewer), carol (operator, outsider)."""
    people = {
        "admin": _user(Role.SYSTEM_ADMIN, "admin"),
        "alice": _user(Role.PIPELINE_OPERATOR, "alice"),
        "bob": _user(Role.VIEWER, "bob"),
        "carol": _user(Role.PIPELINE_OPERATOR, "carol"),
    }
    admin = _as(client, people["admin"][1])
    r = admin.post(
        "/api/v1/workspaces",
        json={"workspace_id": "lab", "display_name": "Lab team"},
    )
    assert r.status_code == 201, r.text
    for who, ws_role in (("alice", "admin"), ("bob", "member")):
        r = admin.post(
            "/api/v1/workspaces/lab/members",
            json={"user_id": people[who][0], "ws_role": ws_role},
        )
        assert r.status_code == 200, r.text
    return people


def _switch(client: TestClient, cookie: str, slug: str) -> None:
    r = _as(client, cookie).put("/api/v1/me/active-workspace", json={"workspace_id": slug})
    assert r.status_code == 200, r.text
    assert r.json()["active"] == slug


# ---------------------------------------------------------------------------
# Migration / defaults
# ---------------------------------------------------------------------------


def test_default_workspace_exists_and_activated_users_join_it(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    ids = {w["workspace_id"] for w in workspaces_svc.list_workspaces()}
    assert {"default", "lab"} <= ids
    for who in ("alice", "bob", "carol"):
        assert workspaces_svc.is_member("default", team[who][0])
    me = _as(client, team["carol"][1]).get("/api/v1/me/workspaces").json()
    assert me["active"] == "default"
    assert [w["workspace_id"] for w in me["workspaces"]] == ["default"]


def test_upgrade_enrols_existing_active_users(isolated_xdg: Path) -> None:
    """A pre-workspace DB: every active user lands in ``default`` once."""
    import sqlite3

    from ovid2.web.settings import web_db_path

    with web_db.connect() as conn:
        conn.execute("DELETE FROM web_workspace_members")
        conn.execute("DELETE FROM web_workspaces")
        conn.execute(
            "INSERT INTO web_users (user_id, issuer, subject, email, name, role, status, "
            "created_at, updated_at) VALUES ('u1','i','s','u1@example.org','u','viewer',"
            "'active','t','t'), ('u2','i','s2','u2@example.org','u','pending','pending','t','t')"
        )
    web_db.ensure_schema()
    conn = sqlite3.connect(web_db_path())
    rows = conn.execute("SELECT workspace_id, user_id FROM web_workspace_members").fetchall()
    conn.close()
    assert ("default", "u1") in rows
    assert ("default", "u2") not in rows


# ---------------------------------------------------------------------------
# Sharing + isolation
# ---------------------------------------------------------------------------


def test_members_share_connections_outsiders_do_not(
    client: TestClient, team: dict[str, tuple[str, str]], isolated_xdg: Path
) -> None:
    # Writing configuration needs MANAGE_CONFIG (system admin) — in lab.
    _switch(client, team["admin"][1], "lab")
    r = _as(client, team["admin"][1]).post(
        "/api/v1/connections/lab_disc",
        json={"type": "disc", "args": {"host_path": str(isolated_xdg)}, "secrets": {}},
    )
    assert r.status_code == 201, r.text
    path = Path(r.json()["path"])
    assert path.parent == workspaces.kind_dir("connections", "lab")

    # alice (operator, member) sees what the admin created in lab.
    _switch(client, team["alice"][1], "lab")
    names = [c["name"] for c in _as(client, team["alice"][1]).get("/api/v1/connections").json()]
    assert "lab_disc" in names

    # carol (default only) neither lists nor fetches it, even by name.
    carol = _as(client, team["carol"][1])
    assert "lab_disc" not in [c["name"] for c in carol.get("/api/v1/connections").json()]
    assert carol.get("/api/v1/connections/lab_disc").status_code == 404
    # …and cannot reach into the workspace with the header (404, not 403).
    r = carol.get("/api/v1/connections", headers={"X-Ovid2-Workspace": "lab"})
    assert r.status_code == 404
    assert r.json()["detail"] == "workspace_not_found"
    # alice's own default view does not see it either.
    _switch(client, team["alice"][1], "default")
    alice_default = _as(client, team["alice"][1]).get("/api/v1/connections").json()
    assert "lab_disc" not in [c["name"] for c in alice_default]


def test_secrets_are_per_workspace(
    client: TestClient, team: dict[str, tuple[str, str]], isolated_xdg: Path
) -> None:
    admin = _as(client, team["admin"][1])
    _switch(client, team["admin"][1], "lab")
    r = admin.post("/api/v1/secrets/lab_token", json={"values": {"token": "s3cr3t"}})
    assert r.status_code in (200, 201), r.text
    target = workspaces.secrets_dir("lab") / "lab_token.json"
    assert target.is_file()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert not (isolated_xdg / "config" / "ovid2" / "secrets.d" / "lab_token.json").exists()
    _switch(client, team["admin"][1], "default")
    names = [s["name"] for s in admin.get("/api/v1/secrets").json()]
    assert "lab_token" not in names


def test_jobs_are_shared_with_members_and_hidden_from_outsiders(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    # Insert a job as if alice had submitted it in ``lab``.
    with workspaces.use_workspace("lab"), web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, submitted_by, pipeline, batch_id, "
            "input_address, output_address, tables_json, transformations_json, "
            "extra_args_json, status, instance_id, workspace_id) "
            "VALUES ('jlab', '2026-01-01T00:00:00', 'alice@example.org', 'p', 'b', 'i', 'o', "
            "'[]', '[]', '{}', 'done', '20260101-000000-aaaaaa', 'lab')"
        )
    _switch(client, team["bob"][1], "lab")
    bob = _as(client, team["bob"][1])
    jobs = bob.get("/api/v1/jobs").json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["jlab"]
    assert jobs[0]["workspace_id"] == "lab"
    assert bob.get("/api/v1/jobs/jlab").status_code == 200

    carol = _as(client, team["carol"][1])
    assert carol.get("/api/v1/jobs").json()["jobs"] == []
    assert carol.get("/api/v1/jobs/jlab").status_code == 404
    # Worker internals still see every workspace.
    assert jobs_svc.get_job_any_workspace("jlab") is not None


def test_global_role_still_applies_inside_the_workspace(
    client: TestClient, team: dict[str, tuple[str, str]], isolated_xdg: Path
) -> None:
    """alice / bob: membership (even ws admin) does not grant MANAGE_CONFIG / data access."""
    _switch(client, team["bob"][1], "lab")
    assert _as(client, team["bob"][1]).get("/api/v1/connections").status_code == 403
    _switch(client, team["alice"][1], "lab")
    r = _as(client, team["alice"][1]).post(
        "/api/v1/connections/nope",
        json={"type": "disc", "args": {"host_path": str(isolated_xdg)}, "secrets": {}},
    )
    assert r.status_code == 403


def test_reports_workspace_share(client: TestClient, team: dict[str, tuple[str, str]]) -> None:
    from ovid2.reports.samples import health_report
    from ovid2.web.services import report_sources_svc, reports_svc

    with workspaces.use_workspace("lab"):
        src = report_sources_svc.store_source(
            __import__("ovid2.reports.sources", fromlist=["ADAPTERS"])
            .ADAPTERS["health"]
            .build(health_report("20260101-000000-bbbbbb", 1)),
            actor="test",
        )
        who = reports_svc.Principal.of(team["alice"][0], "pipeline_operator")
        rid = reports_svc.create_report(who, source_id=src["source_id"])["report_id"]
        reports_svc.set_share(
            rid, who, principal_kind="workspace", principal="*", permission="view"
        )
        # A user outside the workspace cannot be granted.
        with pytest.raises(reports_svc.ReportError):
            reports_svc.set_share(
                rid, who, principal_kind="user", principal=team["carol"][0], permission="view"
            )
    _switch(client, team["bob"][1], "lab")
    assert _as(client, team["bob"][1]).get(f"/api/v1/reports/{rid}").status_code == 200
    assert _as(client, team["carol"][1]).get(f"/api/v1/reports/{rid}").status_code == 404


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


def test_workspace_admin_manages_members_but_cannot_create(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    alice = _as(client, team["alice"][1])
    r = alice.post(
        "/api/v1/workspaces/lab/members", json={"user_id": team["carol"][0], "ws_role": "member"}
    )
    assert r.status_code == 200, r.text
    assert workspaces_svc.is_member("lab", team["carol"][0])
    assert alice.delete(f"/api/v1/workspaces/lab/members/{team['carol'][0]}").status_code == 204
    assert not workspaces_svc.is_member("lab", team["carol"][0])
    r = alice.post("/api/v1/workspaces", json={"workspace_id": "x2", "display_name": "X"})
    assert r.status_code == 403
    # bob is a plain member → cannot manage.
    r = _as(client, team["bob"][1]).post(
        "/api/v1/workspaces/lab/members", json={"user_id": team["carol"][0]}
    )
    assert r.status_code == 403
    # carol is not a member → the workspace does not exist for her.
    assert _as(client, team["carol"][1]).get("/api/v1/workspaces/lab").status_code == 404


def test_removed_member_loses_access_immediately(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    _switch(client, team["bob"][1], "lab")
    workspaces_svc.remove_member("lab", team["bob"][0], actor="test")
    me = _as(client, team["bob"][1]).get("/api/v1/me/workspaces").json()
    assert me["active"] == "default"


def test_archive_requires_second_admin_approval(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    admin = _as(client, team["admin"][1])
    r = admin.post("/api/v1/workspaces/lab/archive")
    assert r.status_code == 202
    approval_id = r.json()["detail"]["approval_id"]
    # The requester cannot approve their own request.
    r = admin.post(f"/api/v1/approvals/{approval_id}/approve", json={"decided_by": "x"})
    assert r.status_code == 403
    second = _user(Role.SYSTEM_ADMIN, "admin2")
    with workspaces.use_workspace(team_ws := "default"):
        assert team_ws == "default"
    r = _as(client, second[1]).post(f"/api/v1/approvals/{approval_id}/approve", json={})
    assert r.status_code == 200, r.text
    r = _as(client, team["admin"][1]).post("/api/v1/workspaces/lab/archive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"
    # Members can no longer switch into it.
    r = _as(client, team["bob"][1]).put("/api/v1/me/active-workspace", json={"workspace_id": "lab"})
    assert r.status_code == 404
    assert approvals_svc.get_approval(approval_id) is not None


def test_html_switcher_and_pages(client: TestClient, team: dict[str, tuple[str, str]]) -> None:
    bob = _as(client, team["bob"][1])
    page = bob.get("/runs").text
    assert "data-ws-switcher" in page and "Lab team" in page
    token = _csrf(bob)
    r = bob.post(
        "/workspaces/switch",
        data={"workspace_id": "lab", "next": "/pipelines/x", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/pipelines"
    assert bob.get("/api/v1/me/workspaces").json()["active"] == "lab"
    # Not visible → 404.
    r = _as(client, team["carol"][1]).post(
        "/workspaces/switch",
        data={"workspace_id": "lab", "csrf_token": _csrf(_as(client, team["carol"][1]))},
        follow_redirects=False,
    )
    assert r.status_code == 404
    # Admin pages.
    assert _as(client, team["admin"][1]).get("/workspaces").status_code == 200
    assert _as(client, team["alice"][1]).get("/workspaces/lab").status_code == 200
    assert _as(client, team["bob"][1]).get("/workspaces/lab").status_code == 404
    assert _as(client, team["bob"][1]).get("/workspaces").status_code == 403


def test_actor_is_the_real_user(client: TestClient, team: dict[str, tuple[str, str]]) -> None:
    alice = _as(client, team["alice"][1])
    alice.post(
        "/api/v1/workspaces/lab/members", json={"user_id": team["carol"][0], "ws_role": "member"}
    )
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT actor, workspace_id FROM web_audit WHERE action = 'workspace.member_add' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["actor"] == "alice@example.org"
    assert row["workspace_id"] == "lab"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_assistant_token_is_pinned_to_its_workspace(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    token = llm_token.mint(
        team["bob"][0], Role.VIEWER, email="bob@example.org", ttl_seconds=60, workspace_id="lab"
    )
    client.cookies.clear()
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        assert client.get("/api/v1/me/workspaces", headers=hdr).json()["active"] == "lab"
        # The header cannot move it elsewhere.
        hdr2 = {**hdr, "X-Ovid2-Workspace": "default"}
        assert client.get("/api/v1/me/workspaces", headers=hdr2).json()["active"] == "lab"
    finally:
        llm_token.revoke(token)


def test_service_token_bound_to_workspaces(
    client: TestClient, team: dict[str, tuple[str, str]], isolated_xdg: Path
) -> None:
    secrets = isolated_xdg / "config" / "ovid2" / "secrets.d"
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = "svc-token-for-tests"
    digest = hashlib.sha256(token.encode()).hexdigest()
    path = secrets / "auth_service_tokens.json"
    path.write_text(
        json.dumps({digest: {"role": "viewer", "workspaces": ["lab"]}}), encoding="utf-8"
    )
    os.chmod(path, 0o600)
    client.cookies.clear()
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/me/workspaces", headers=hdr).json()["active"] == "lab"
    r = client.get("/api/v1/jobs", headers={**hdr, "X-Ovid2-Workspace": "default"})
    assert r.status_code == 404
    r = client.get("/api/v1/jobs", headers={**hdr, "X-Ovid2-Workspace": "lab"})
    assert r.status_code == 200
