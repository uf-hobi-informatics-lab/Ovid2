"""Server-side, per-user appearance preferences (``/api/v1/me/appearance``).

The Appearance panel's theme, quick colours, per-component overrides and
saved themes are stored per user in ``web_user_prefs`` so the look follows
the user to any browser. These tests cover:

- ``prefs_svc`` validation (catalogue-derived patterns) and round trip;
- the GET / PUT API, including per-user isolation with auth on and the
  403 for machine principals (assistant token) and the 415 JSON gate;
- server rendering of the stored look into the pre-paint script on every
  full page (base / login), so no browser storage is involved.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth import llm_token
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import prefs_svc, sessions_svc, settings_svc, users_svc

_FINANCIAL = {
    "theme": "financial",
    "colors": {"accent": "#ff8800"},
    "appearance": {"--body-fs": "16px", "--nav-ff": "Georgia, serif", "--drawer-bg": "#102030"},
    "saved_themes": [
        {"name": "desk", "theme": "financial", "overrides": {"--accent": "#ff8800"}},
    ],
}


@pytest.fixture(autouse=True)
def _temp_db(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    prefs_svc._SCHEMA_READY.clear()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    c = TestClient(create_app())
    try:
        yield c
    finally:
        c.close()


def _enable_auth(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _cookie(role: Role, subject: str) -> tuple[str, str]:
    """Create an active user + session; return ``(user_id, signed_cookie)``."""
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


def _prepaint(body: str) -> dict[str, object]:
    marker = "window.OVID2_APPEARANCE = "
    start = body.index(marker) + len(marker)
    end = body.index(";\n", start)
    return json.loads(body[start:end])  # type: ignore[no-any-return]


# ---------------------------------------------------------------- service


@pytest.mark.unit
def test_defaults_when_never_saved() -> None:
    assert prefs_svc.get_appearance("nobody") == prefs_svc.default_appearance()


@pytest.mark.unit
def test_round_trip_and_overwrite() -> None:
    stored = prefs_svc.save_appearance("u1", _FINANCIAL)
    assert stored == prefs_svc.get_appearance("u1")
    assert stored["theme"] == "financial"
    assert stored["saved_themes"][0]["name"] == "desk"
    prefs_svc.save_appearance("u1", {"theme": "lego"})
    assert prefs_svc.get_appearance("u1")["theme"] == "lego"
    assert prefs_svc.get_appearance("u1")["saved_themes"] == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"theme": "not-a-theme"},
        {"colors": {"accent": "red"}},
        {"colors": {"shadow": "#000000"}},
        {"appearance": {"--evil": "#000000"}},
        {"appearance": {"--bg": "url(javascript:alert(1))"}},
        {"appearance": {"--body-ff": "x; background: url(a)"}},
        {"appearance": {"--body-fs": "9999px"}},
        {"saved_themes": [{"name": "<b>", "theme": "lego", "overrides": {}}]},
        {"saved_themes": [{"name": "a", "theme": "nope", "overrides": {}}]},
        {"saved_themes": [{"name": "a", "theme": "lego"}, {"name": "a", "theme": "lego"}]},
        {"saved_themes": [{"name": f"t{i}", "theme": "lego"} for i in range(31)]},
        {"extra": 1},
    ],
)
def test_validation_rejects(payload: dict[str, object]) -> None:
    with pytest.raises(prefs_svc.PrefsError):
        prefs_svc.validate(payload)


@pytest.mark.unit
def test_validation_accepts_every_catalogue_var() -> None:
    """Every var the browser may store passes (catalogue + derived shades)."""
    doc = prefs_svc.validate(
        {
            "appearance": {
                "--accent": "#16b877",
                "--accent-hover": "#45c692",
                "--accent-subtle": "rgba(22, 184, 119, 0.14)",
                "--accent-fg": "#14181f",
                "--body-ff": "var(--font-serif)",
                "--btn-fw": "600",
                "--table-fs": "0.95rem",
            }
        }
    )
    assert len(doc["appearance"]) == 7


@pytest.mark.unit
def test_corrupt_row_degrades_to_defaults() -> None:
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_user_prefs (user_id, appearance_json, updated_at) VALUES (?, ?, ?)",
            ("u2", '{"theme": "retired-theme"}', "now"),
        )
    assert prefs_svc.get_appearance("u2") == prefs_svc.default_appearance()


# -------------------------------------------------------------------- API


@pytest.mark.unit
def test_api_round_trip_auth_off_shared_profile(client: TestClient) -> None:
    """Auth off: every browser is ``anonymous`` -> one shared profile."""
    assert client.get("/api/v1/me/appearance").json()["theme"] is None
    r = client.put("/api/v1/me/appearance", json=_FINANCIAL)
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == "financial"
    # A second, cookie-less client (another laptop) sees the same look.
    with TestClient(create_app()) as other:
        got = other.get("/api/v1/me/appearance").json()
    assert got["theme"] == "financial"
    assert got["colors"] == {"accent": "#ff8800"}
    assert got["saved_themes"][0]["name"] == "desk"


@pytest.mark.unit
def test_api_accepts_spaceship_theme(client: TestClient) -> None:
    """The Spaceship slug is in the catalogue enum, so the API accepts it."""
    r = client.put("/api/v1/me/appearance", json={"theme": "spaceship"})
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == "spaceship"
    assert client.get("/api/v1/me/appearance").json()["theme"] == "spaceship"
    assert _prepaint(client.get("/runs").text)["theme"] == "spaceship"


@pytest.mark.unit
def test_api_rejects_bad_values(client: TestClient) -> None:
    r = client.put("/api/v1/me/appearance", json={"appearance": {"--evil": "#000000"}})
    assert r.status_code == 422


@pytest.mark.unit
def test_api_per_user_isolation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    _, alice = _cookie(Role.VIEWER, "alice")
    bob_id, bob = _cookie(Role.GUEST, "bob")
    client.cookies.set("ovid2_session", alice)
    assert client.put("/api/v1/me/appearance", json=_FINANCIAL).status_code == 200
    client.cookies.clear()
    client.cookies.set("ovid2_session", bob)
    assert client.get("/api/v1/me/appearance").json()["theme"] is None
    assert client.put("/api/v1/me/appearance", json={"theme": "lego"}).status_code == 200
    assert prefs_svc.get_appearance(bob_id)["theme"] == "lego"
    client.cookies.clear()
    client.cookies.set("ovid2_session", alice)
    assert client.get("/api/v1/me/appearance").json()["theme"] == "financial"


@pytest.mark.unit
def test_api_requires_login_when_auth_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    assert client.get("/api/v1/me/appearance").status_code == 401
    assert client.put("/api/v1/me/appearance", json={"theme": "lego"}).status_code == 401


@pytest.mark.unit
def test_api_put_requires_json_when_auth_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    _, alice = _cookie(Role.VIEWER, "carol")
    client.cookies.set("ovid2_session", alice)
    r = client.put(
        "/api/v1/me/appearance",
        content=json.dumps({"theme": "lego"}),
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 415


@pytest.mark.unit
def test_assistant_token_cannot_read_or_write(client: TestClient) -> None:
    token = llm_token.mint("anonymous", Role.VIEWER, email=None, ttl_seconds=60)
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        assert client.get("/api/v1/me/appearance", headers=hdr).status_code == 403
        assert (
            client.put("/api/v1/me/appearance", json={"theme": "lego"}, headers=hdr).status_code
            == 403
        )
    finally:
        llm_token.revoke(token)


# ------------------------------------------------------------- rendering


@pytest.mark.unit
def test_pages_render_stored_look_before_css(client: TestClient) -> None:
    """Every full page carries the server-stored look ahead of the CSS."""
    prefs_svc.save_appearance("anonymous", _FINANCIAL)
    for path in ("/", "/runs", "/settings", "/pipelines"):
        body = client.get(path).text
        assert body.index("window.OVID2_APPEARANCE") < body.index("/static/ovid2.css"), path
        got = _prepaint(body)
        assert got["theme"] == "financial", path
        assert got["appearance"] == _FINANCIAL["appearance"], path


@pytest.mark.unit
def test_prepaint_never_renders_unvalidated_rows(client: TestClient) -> None:
    """A tampered row cannot inject markup: it fails validation -> defaults."""
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_user_prefs (user_id, appearance_json, updated_at) VALUES (?, ?, ?)",
            (
                "anonymous",
                json.dumps({"appearance": {"--bg": "</script><script>alert(1)</script>"}}),
                "now",
            ),
        )
    body = client.get("/").text
    assert "alert(1)" not in body
    assert _prepaint(body) == prefs_svc.default_appearance()


@pytest.mark.unit
def test_login_page_uses_defaults(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    prefs_svc.save_appearance("anonymous", _FINANCIAL)
    body = client.get("/login").text
    assert body.index("window.OVID2_APPEARANCE") < body.index("/static/ovid2.css")
    assert _prepaint(body)["theme"] is None


@pytest.mark.unit
def test_signed_in_user_page_renders_their_look(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    uid, cookie = _cookie(Role.VIEWER, "dana")
    prefs_svc.save_appearance(uid, {"theme": "tatooine"})
    client.cookies.set("ovid2_session", cookie)
    assert _prepaint(client.get("/runs").text)["theme"] == "tatooine"
