"""Report Studio web surface: library, ACL / sharing, versions, ops, redaction, export.

Covers ``reports_svc`` / ``report_sources_svc`` through the real REST
routes (``/api/v1/report*``), with auth both off (single anonymous user)
and on (real users + signed session cookies, role-gated), plus the
worker's post-run ingest hook and the HTML pages.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from ovid2.reports.samples import analysis_records, health_report
from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import (
    report_sources_svc,
    reports_svc,
    sessions_svc,
    settings_svc,
    users_svc,
)

IID = "20260101-000000-abcdef"
IID2 = "20260102-000000-abcde2"


@pytest.fixture(autouse=True)
def _env(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ovid2.config.settings as settings

    monkeypatch.setattr(settings, "WORK_DIR", Path(isolated_xdg / "runtime" / "ovid2"))
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


def _work() -> Path:
    import ovid2.config.settings as settings

    return Path(settings.WORK_DIR)


def _write_health(iid: str = IID, passed: int = 1) -> None:
    (_work() / f"health-report-{iid}.json").write_text(
        json.dumps(health_report(iid, passed)), encoding="utf-8"
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    c = TestClient(create_app())
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def source_id(client: TestClient) -> str:
    _write_health()
    r = client.post("/api/v1/report-sources/ingest", json={"instance_id": IID})
    assert r.status_code == 200, r.text
    return r.json()["sources"][0]["source_id"]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_ingest_is_durable_private_and_idempotent(client: TestClient, source_id: str) -> None:
    assert source_id == f"health-{IID}"
    path = report_sources_svc.sources_dir() / f"{source_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    # WORK_DIR is tmpfs in production: the source survives the artifact.
    (_work() / f"health-report-{IID}.json").unlink()
    assert client.get(f"/api/v1/report-sources/{source_id}").status_code == 200
    _write_health()
    again = client.post("/api/v1/report-sources/ingest", json={"instance_id": IID}).json()
    assert [s["source_id"] for s in again["sources"]] == [source_id]
    assert client.get("/api/v1/report-sources").json()["total"] == 1


def test_source_description_has_no_rows(client: TestClient, source_id: str) -> None:
    d = client.get(f"/api/v1/report-sources/{source_id}").json()
    assert "summary" in d["datasets"]
    assert all("rows" not in ds for ds in d["datasets"].values())


def test_trend_source(client: TestClient, source_id: str) -> None:
    _write_health(IID2, passed=2)
    sid2 = client.post("/api/v1/report-sources/ingest", json={"instance_id": IID2}).json()[
        "sources"
    ][0]["source_id"]
    r = client.post("/api/v1/report-sources/trend", json={"source_ids": [source_id, sid2]})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "health_trend"


def test_unknown_source_404(client: TestClient) -> None:
    assert client.get("/api/v1/report-sources/health-nope").status_code == 404
    assert client.post("/api/v1/reports", json={"source_id": "health-nope"}).status_code == 400


def test_worker_finalize_ingests(isolated_xdg: Path) -> None:
    from ovid2.web.worker import _ingest_report_sources

    _write_health()
    (_work() / f"analysis-{IID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in analysis_records()) + "\n", encoding="utf-8"
    )
    _ingest_report_sources(IID)
    kinds = {s["kind"] for s in report_sources_svc.list_sources()["sources"]}
    assert {"health", "analysis"} <= kinds
    _ingest_report_sources("not-an-instance")  # never raises


# ---------------------------------------------------------------------------
# Library, versions, ops (auth off: one anonymous principal)
# ---------------------------------------------------------------------------


def test_create_from_recommended_template(client: TestClient, source_id: str) -> None:
    r = client.post(
        "/api/v1/reports",
        json={"source_id": source_id, "folder": "EDC//2026/..", "tags": ["EDC", "edc", "q1"]},
    )
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["template_id"] == "health.edc_summary"
    assert rep["folder"] == "EDC/2026" and rep["tags"] == ["edc", "q1"]
    assert rep["permission"] == "owner" and rep["version"] == 1
    lib = client.get("/api/v1/reports").json()
    assert lib["total"] == 1 and lib["folders"] == [{"folder": "EDC/2026", "count": 1}]


def test_create_rejects_invalid_layout(client: TestClient, source_id: str) -> None:
    bad = {
        "v": 1,
        "title": "T",
        "sections": [
            {
                "id": "s",
                "title": "S",
                "blocks": [
                    {
                        "id": "b",
                        "type": "chart",
                        "chart": "bar",
                        "dataset": "status",
                        "params": {"category": "status", "value": "nope"},
                    }
                ],
            }
        ],
    }
    r = client.post("/api/v1/reports", json={"source_id": source_id, "layout": bad})
    assert r.status_code == 400
    assert any("no column 'nope'" in e for e in r.json()["detail"]["errors"])


def test_ops_preview_save_versions_restore(client: TestClient, source_id: str) -> None:
    rep = client.post("/api/v1/reports", json={"source_id": source_id}).json()
    rid = rep["report_id"]
    ops = [
        {"op": "set_visibility", "target_id": "status", "visible": False},
        {"op": "move_section", "section_id": "action", "index": 0},
    ]
    pv = client.post(
        f"/api/v1/reports/{rid}/ops",
        json={"ops": ops, "save": False, "render": True, "layout": rep["layout"]},
    )
    assert pv.status_code == 200, pv.text
    assert [s["id"] for s in pv.json()["model"]["sections"]] == ["action", "overview"]
    assert client.get(f"/api/v1/reports/{rid}").json()["version"] == 1  # preview never saves
    saved = client.put(
        f"/api/v1/reports/{rid}", json={"layout": pv.json()["layout"], "expected_version": 1}
    )
    assert saved.status_code == 200 and saved.json()["version"] == 2
    stale = client.put(f"/api/v1/reports/{rid}", json={"title": "x", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == 2
    assert [v["version"] for v in client.get(f"/api/v1/reports/{rid}/versions").json()] == [2, 1]
    back = client.post(f"/api/v1/reports/{rid}/versions/1/restore", json={})
    assert back.status_code == 200 and back.json()["version"] == 3
    assert back.json()["layout"]["sections"][0]["id"] == "overview"


def test_ops_rejected_with_errors(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    r = client.post(
        f"/api/v1/reports/{rid}/ops",
        json={"ops": [{"op": "remove_section", "section_id": "ghost"}]},
    )
    assert r.status_code == 400 and "no section" in r.json()["detail"]["errors"][0]


def test_favorites_archive_duplicate_templates(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    client.put(f"/api/v1/reports/{rid}/favorite", json={"favorite": True})
    assert client.get("/api/v1/reports?scope=favorites").json()["total"] == 1
    dup = client.post(f"/api/v1/reports/{rid}/duplicate", json={"title": "Copy"}).json()
    assert dup["title"] == "Copy" and dup["report_id"] != rid
    tpl = client.post(f"/api/v1/reports/{rid}/save-as-template", json={"name": "Mine"}).json()
    assert tpl["template_id"].startswith("user.")
    ids = [
        t["template_id"] for t in client.get("/api/v1/report-templates?source_kind=health").json()
    ]
    assert tpl["template_id"] in ids and "health.edc_summary" in ids
    made = client.post(
        "/api/v1/reports", json={"source_id": source_id, "template_id": tpl["template_id"]}
    )
    assert made.status_code == 201
    assert client.post(f"/api/v1/reports/{rid}/archive", json={}).status_code == 200
    assert rid not in {r["report_id"] for r in client.get("/api/v1/reports").json()["reports"]}
    assert client.get("/api/v1/reports?archived=true").json()["total"] == 1
    assert client.post(f"/api/v1/reports/{rid}/restore", json={}).status_code == 200
    assert client.delete(f"/api/v1/report-templates/{tpl['template_id']}").status_code == 200


def test_report_draft_route(client: TestClient, source_id: str) -> None:
    r = client.post("/api/v1/report-drafts", json={"source_id": source_id})
    assert r.status_code == 201
    assert r.json()["status"] == "draft" and r.json()["url"] == f"/reports/{r.json()['report_id']}"


def test_audit_trail(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    client.put(
        f"/api/v1/reports/{rid}/shares",
        json={"principal_kind": "role", "principal": "viewer", "permission": "view"},
    )
    client.delete(f"/api/v1/reports/{rid}/shares/role/viewer")
    with web_db.connect() as conn:
        actions = [
            r[0]
            for r in conn.execute(
                "SELECT action FROM web_audit WHERE target = ? ORDER BY ts", (rid,)
            )
        ]
    assert actions == ["report.create", "report.share", "report.unshare"]


def test_html_pages(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    lib = client.get("/reports")
    assert lib.status_code == 200 and "report_library.js" in lib.text
    page = client.get(f"/reports/{rid}")
    assert page.status_code == 200
    for asset in (
        "vega.bundle.min.js",
        "report_render.js",
        "report_studio.js",
        "report_render.css",
    ):
        assert asset in page.text
    assert client.get("/reports/rpt_missing").status_code == 404
    assert 'data-nav="reports"' in client.get("/").text


def test_studio_has_autosave_toggle(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    page = client.get(f"/reports/{rid}").text
    head = page.split('class="rs-studio-head"', 1)[1].split("</header>", 1)[0]
    assert 'type="checkbox" role="switch" data-autosave' in head
    assert "data-save-state" in head and "data-save" in head


def test_autosave_versions_are_noted(client: TestClient, source_id: str) -> None:
    rep = client.post("/api/v1/reports", json={"source_id": source_id}).json()
    rid = rep["report_id"]
    lay = dict(rep["layout"], title="Autosaved title")
    r = client.put(
        f"/api/v1/reports/{rid}",
        json={"layout": lay, "expected_version": 1, "note": "Autosave"},
    )
    assert r.status_code == 200 and r.json()["version"] == 2
    latest = client.get(f"/api/v1/reports/{rid}/versions").json()[0]
    assert latest["version"] == 2 and latest["note"] == "Autosave"


# ---------------------------------------------------------------------------
# Export (real executor subprocess)
# ---------------------------------------------------------------------------


def test_catalogue_lists_pptx(client: TestClient) -> None:
    fmts = {f["format"] for f in client.get("/api/v1/report-catalogue").json()["export_formats"]}
    assert "pptx" in fmts


def test_export_pptx(client: TestClient, source_id: str) -> None:
    pptx = pytest.importorskip("pptx")
    pytest.importorskip("vl_convert")
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    r = client.post(f"/api/v1/reports/{rid}/exports", json={"format": "pptx"})
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["filename"].endswith(".pptx")
    dl = client.get(meta["download_url"])
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    prs = pptx.Presentation(io.BytesIO(dl.content))
    assert len(prs.slides) >= 2


def test_export_json_roundtrip(client: TestClient, source_id: str) -> None:
    rid = client.post("/api/v1/reports", json={"source_id": source_id}).json()["report_id"]
    r = client.post(f"/api/v1/reports/{rid}/exports", json={"format": "json"})
    assert r.status_code == 201, r.text
    meta = r.json()
    dl = client.get(meta["download_url"])
    assert dl.status_code == 200 and dl.headers["cache-control"] == "no-store"
    doc = json.loads(dl.content)
    assert doc["layout"]["title"] and doc["source"]["source_id"] == source_id
    assert doc["report"]["report_id"] == rid and doc["report"]["report_version"] == 1
    ex = reports_svc.export_dir() / meta["export_id"]
    assert not (ex / "bundle.json").exists()  # bundle is removed after rendering
    assert ex.stat().st_mode & 0o777 == 0o700


def test_export_pdf_skips_hidden(client: TestClient, source_id: str) -> None:
    pytest.importorskip("reportlab")
    pytest.importorskip("vl_convert")
    pytest.importorskip("svglib")
    rep = client.post("/api/v1/reports", json={"source_id": source_id}).json()
    hidden, _ = __import__("ovid2.reports.layout", fromlist=["apply_ops"]).apply_ops(
        rep["layout"],
        [
            {
                "op": "update_section",
                "section_id": "action",
                "set": {"title": "SECRETSECTION", "visible": False},
            }
        ],
    )
    r = client.post(
        f"/api/v1/reports/{rep['report_id']}/exports", json={"format": "html", "layout": hidden}
    )
    assert r.status_code == 201, r.text
    html = client.get(r.json()["download_url"]).text
    assert "SECRETSECTION" not in html


# ---------------------------------------------------------------------------
# Auth on: ACL, sharing, roles, redaction
# ---------------------------------------------------------------------------


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = GlobalSettings(auth=SettingsAuth(enabled=True))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _cookie(role: Role, subject: str) -> tuple[str, str]:
    from http.cookies import SimpleCookie

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


def test_acl_private_by_default_then_shared(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_health()
    report_sources_svc.ingest_for_instance(IID)
    sid = f"health-{IID}"
    _enable_auth(monkeypatch)
    alice_id, alice = _cookie(Role.PIPELINE_OPERATOR, "alice")
    bob_id, bob = _cookie(Role.VIEWER, "bob")
    _gid, guest = _cookie(Role.GUEST, "gail")
    rid = _as(client, alice).post("/api/v1/reports", json={"source_id": sid}).json()["report_id"]

    # Private: invisible (404, not 403) to bob; guests lack VIEW_RUNS entirely.
    assert _as(client, bob).get(f"/api/v1/reports/{rid}").status_code == 404
    assert _as(client, bob).get("/api/v1/reports").json()["total"] == 0
    assert _as(client, guest).get("/api/v1/reports").status_code == 403

    # Share view with bob: he can read and render, not edit, not reshare.
    r = _as(client, alice).put(
        f"/api/v1/reports/{rid}/shares",
        json={"principal_kind": "user", "principal": bob_id, "permission": "view"},
    )
    assert r.status_code == 200
    assert _as(client, bob).get(f"/api/v1/reports/{rid}").json()["permission"] == "view"
    assert _as(client, bob).get(f"/api/v1/reports/{rid}/render").status_code == 200
    assert _as(client, bob).get("/api/v1/reports?scope=shared").json()["total"] == 1
    assert _as(client, bob).put(f"/api/v1/reports/{rid}", json={"title": "x"}).status_code == 403
    assert (
        _as(client, bob)
        .put(
            f"/api/v1/reports/{rid}/shares", json={"principal_kind": "role", "principal": "viewer"}
        )
        .status_code
        == 403
    )
    assert _as(client, bob).get(f"/api/v1/reports/{rid}").json()["shares"] == []

    # Upgrade to edit: bob saves; still cannot archive or file it.
    _as(client, alice).put(
        f"/api/v1/reports/{rid}/shares",
        json={"principal_kind": "user", "principal": bob_id, "permission": "edit"},
    )
    assert (
        _as(client, bob).put(f"/api/v1/reports/{rid}", json={"title": "Bob edit"}).status_code
        == 200
    )
    assert _as(client, bob).put(f"/api/v1/reports/{rid}", json={"folder": "x"}).status_code == 403
    assert _as(client, bob).post(f"/api/v1/reports/{rid}/archive", json={}).status_code == 403

    # Revoke: gone again.
    _as(client, alice).delete(f"/api/v1/reports/{rid}/shares/user/{bob_id}")
    assert _as(client, bob).get(f"/api/v1/reports/{rid}").status_code == 404
    assert alice_id != bob_id


def test_role_share_and_html_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_health()
    report_sources_svc.ingest_for_instance(IID)
    _enable_auth(monkeypatch)
    _a, alice = _cookie(Role.PIPELINE_OPERATOR, "alice")
    _b, bob = _cookie(Role.VIEWER, "bob")
    rid = (
        _as(client, alice)
        .post("/api/v1/reports", json={"source_id": f"health-{IID}"})
        .json()["report_id"]
    )
    assert _as(client, bob).get(f"/reports/{rid}").status_code == 404
    _as(client, alice).put(
        f"/api/v1/reports/{rid}/shares", json={"principal_kind": "role", "principal": "viewer"}
    )
    assert _as(client, bob).get(f"/reports/{rid}").status_code == 200
    bad = _as(client, alice).put(
        f"/api/v1/reports/{rid}/shares", json={"principal_kind": "role", "principal": "pending"}
    )
    assert bad.status_code == 400


def test_exports_are_per_user(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_health()
    report_sources_svc.ingest_for_instance(IID)
    _enable_auth(monkeypatch)
    _a, alice = _cookie(Role.PIPELINE_OPERATOR, "alice")
    _b, bob = _cookie(Role.VIEWER, "bob")
    rid = (
        _as(client, alice)
        .post("/api/v1/reports", json={"source_id": f"health-{IID}"})
        .json()["report_id"]
    )
    url = (
        _as(client, alice)
        .post(f"/api/v1/reports/{rid}/exports", json={"format": "json"})
        .json()["download_url"]
    )
    assert _as(client, alice).get(url).status_code == 200
    assert _as(client, bob).get(url).status_code == 404


def test_row_values_redacted_for_viewers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_work() / f"analysis-{IID}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in analysis_records()) + "\n", encoding="utf-8"
    )
    stored = report_sources_svc.ingest_for_instance(IID)
    sid = next(s["source_id"] for s in stored if s["kind"] == "analysis")
    layout = {
        "v": 1,
        "title": "Values",
        "sections": [
            {
                "id": "s",
                "title": "S",
                "blocks": [
                    {
                        "id": "t",
                        "type": "chart",
                        "chart": "table",
                        "dataset": "top_values",
                        "params": {"columns": ["column", "value", "records"]},
                    }
                ],
            }
        ],
    }
    _enable_auth(monkeypatch)
    _a, alice = _cookie(Role.PIPELINE_OPERATOR, "alice")
    _b, bob = _cookie(Role.VIEWER, "bob")
    rid = (
        _as(client, alice)
        .post("/api/v1/reports", json={"source_id": sid, "layout": layout})
        .json()["report_id"]
    )
    _as(client, alice).put(
        f"/api/v1/reports/{rid}/shares",
        json={"principal_kind": "user", "principal": _b, "permission": "view"},
    )

    def cells(cookie: str) -> list:
        m = _as(client, cookie).get(f"/api/v1/reports/{rid}/render").json()
        return [c for row in m["sections"][0]["blocks"][0]["table"]["rows"] for c in row]

    assert "AV" in cells(alice)  # operator holds VIEW_PIPELINE_DATA
    assert "AV" not in cells(bob) and reports_svc.REDACTED in cells(bob)
    desc = _as(client, bob).get(f"/api/v1/report-sources/{sid}").json()
    assert desc["row_values_redacted"] is True
