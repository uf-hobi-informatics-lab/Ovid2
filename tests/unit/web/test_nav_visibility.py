"""Permission-reflected nav / palette visibility tests (Wave 3, todo 16).

Exercises the role-aware navigation drawer, the user chip + Logout link, and
the capability-filtered command palette rendered by ``base.html`` /
``dashboard.html`` through a FastAPI ``TestClient``. The cookie / session setup
mirrors ``test_rest_gating.py``; ``settings_svc.get_settings`` is
monkeypatched to toggle ``auth.enabled``.

Covered acceptance criteria:

* guest GET ``/`` HTML omits the ``/secrets`` / ``/settings`` / ``/submit``
  nav links and shows a Logout link.
* admin GET ``/`` shows every nav group.
* the Ctrl-K palette is nav-derived (no server-side roster).
* enabled=False GET ``/`` shows all nav (no regression).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth.roles import Role
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import sessions_svc, settings_svc, users_svc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` that does not auto-follow redirects."""
    client = TestClient(create_app(), follow_redirects=False)
    try:
        yield client
    finally:
        client.close()


def _enable_auth(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force ``settings_svc.get_settings`` to a known ``auth.enabled`` value."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _signed_cookie_value(session_id: str) -> str:
    """Return the signed cookie value ``cookie.issue_cookie`` would emit."""
    from http.cookies import SimpleCookie

    response = Response()
    cookie_mod.issue_cookie(response, session_id)
    line = next(
        value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"
    )
    jar: SimpleCookie[str] = SimpleCookie()
    jar.load(line)
    return jar["ovid2_session"].value


def _cookie_for_role(role: Role, subject: str) -> str:
    """Create an active user in ``role`` + session; return its signed cookie."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
    record = users_svc.activate(user_id=record["user_id"], role=role, actor="test")
    session = sessions_svc.create(
        user_id=str(record["user_id"]),
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=45,
    )
    return _signed_cookie_value(session.session_id)


def _set_cookie(client: TestClient, cookie: str | None) -> None:
    """Set (or clear) the ``ovid2_session`` cookie on the client instance."""
    client.cookies.clear()
    if cookie is not None:
        client.cookies.set("ovid2_session", cookie)


def _nav_region(body: str) -> str:
    """Return just the primary nav drawer markup from a rendered page.

    The dashboard page body also contains contextual ``/submit`` etc. links
    (help text + tiles) that are *not* the nav; scoping assertions to the
    ``<aside id="site-nav">`` drawer keeps the visibility checks precise.
    """
    import re

    match = re.search(r'<aside id="site-nav".*?</aside>', body, re.DOTALL)
    assert match is not None, "nav drawer not found in page"
    return match.group(0)


# ---------------------------------------------------------------------------
# guest — reduced nav + Logout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guest_nav_omits_privileged_links(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A guest sees only VIEW_PUBLIC nav — no secrets/settings/submit links."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.GUEST, "guest-nav"))
    nav = _nav_region(app_client.get("/").text)
    assert 'href="/secrets"' not in nav
    assert 'href="/settings"' not in nav
    assert 'href="/submit"' not in nav
    assert 'href="/install"' not in nav
    assert 'href="/warehouses"' not in nav
    assert 'href="/storage"' not in nav
    # Public pages remain reachable.
    assert 'href="/cdms"' in nav
    assert 'href="/health"' in nav


@pytest.mark.unit
def test_guest_sees_logout_link(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """A resolved (guest) user gets a Logout link in the topbar."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.GUEST, "guest-logout"))
    body = app_client.get("/").text
    assert 'href="/logout"' in body


# ---------------------------------------------------------------------------
# admin — full nav
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_admin_nav_shows_all_groups(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An admin sees every capability-gated nav link."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.SYSTEM_ADMIN, "admin-nav"))
    nav = _nav_region(app_client.get("/").text)
    for link in (
        'href="/submit"',
        'href="/pipelines"',
        'href="/runs"',
        'href="/pipelines"',
        'href="/dataspace"',
        'href="/storage"',
        'href="/warehouses"',
        'href="/secrets"',
        'href="/settings"',
        'href="/install"',
    ):
        assert link in nav, f"admin nav missing {link}"


# ---------------------------------------------------------------------------
# command palette filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_landing_has_no_server_palette_roster(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """The palette is built client-side from the nav (``cmdk.js``).

    The console's server-side palette JSON was removed with the console;
    RBAC filtering of palette targets is inherited from the nav drawer,
    which the guest/admin tests above already cover.
    """
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.GUEST, "guest-palette"))
    body = app_client.get("/").text
    assert "ovid2-palette-data" not in body
    assert 'id="command-palette"' in body
    assert "/static/cmdk.js" in body


# ---------------------------------------------------------------------------
# disabled — no regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_auth_shows_all_nav(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth.enabled=False → every nav link present (today's behaviour)."""
    _enable_auth(monkeypatch, enabled=False)
    _set_cookie(app_client, None)
    body = app_client.get("/").text
    nav = _nav_region(body)
    for link in (
        'href="/submit"',
        'href="/secrets"',
        'href="/settings"',
        'href="/install"',
        'href="/warehouses"',
    ):
        assert link in nav, f"open-auth nav missing {link}"
    # The user chip / Logout are hidden when auth is disabled.
    assert 'href="/logout"' not in body
    # Footer keeps the "No auth" reminder.
    assert "No auth" in body


@pytest.mark.unit
def test_enabled_footer_drops_no_auth_line(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """With auth enabled the footer no longer says 'No auth'."""
    _enable_auth(monkeypatch, enabled=True)
    _set_cookie(app_client, _cookie_for_role(Role.SYSTEM_ADMIN, "admin-footer"))
    body = app_client.get("/").text
    assert "No auth" not in body


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
