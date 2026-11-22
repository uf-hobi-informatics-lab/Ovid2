"""Unit tests for CSRF protection (Wave 3, todo 15).

Covers the plan's acceptance criteria for :mod:`ovid2.web.auth.csrf`:

- A mutating form POST without a valid token → ``403`` (auth enabled).
- The same POST carrying a template-issued token → passes.
- ``auth.enabled=False`` → no CSRF enforced (POST passes without a token).

Exercised both directly (``validate_token`` / ``require_csrf`` semantics) and
through a small wired FastAPI route driven by ``TestClient``. The signing key
comes from the shared ``cookie`` module, so its in-process cache is reset per
test to keep the isolated XDG tree authoritative.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Form
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.auth import csrf
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import settings_svc
from ovid2.web.templating import templates


@pytest.fixture(autouse=True)
def _reset_key_cache(isolated_xdg: Path) -> None:
    """Reset the shared signing-key cache around each test."""
    cookie_mod.reset_signing_key_cache()


def _use_auth(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force ``settings_svc.get_settings`` to a known ``auth.enabled`` value."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


def _wired_client() -> TestClient:
    """Return a ``TestClient`` for a minimal app with a CSRF-gated route."""
    app = FastAPI()

    @app.post("/thing")
    async def _thing(
        value: str = Form(""),
        _csrf: None = Depends(csrf.require_csrf),
    ) -> PlainTextResponse:
        return PlainTextResponse(f"ok:{value}")

    return TestClient(app)


@pytest.mark.unit
def test_issue_then_validate_round_trip(isolated_xdg: Path) -> None:
    """A freshly issued token validates."""
    token = csrf.issue_token()
    assert csrf.validate_token(token) is True


@pytest.mark.unit
def test_validate_rejects_absent_and_tampered(isolated_xdg: Path) -> None:
    """Absent, empty, and tampered tokens fail validation."""
    assert csrf.validate_token(None) is False
    assert csrf.validate_token("") is False

    token = csrf.issue_token()
    # Tamper the *first* character of the signature segment (after the final
    # ``.``) rather than the last: URL-safe base64's trailing character can
    # carry unused low-order bits, so flipping it may decode to the identical
    # byte string and still verify. Flipping a leading signature character
    # always changes a full signature byte, reliably breaking the HMAC.
    head, _, signature = token.rpartition(".")
    assert head and signature, "expected a payload.timestamp.signature token"
    swapped = ("A" if signature[0] != "A" else "B") + signature[1:]
    flipped = f"{head}.{swapped}"
    assert flipped != token
    assert csrf.validate_token(flipped) is False


@pytest.mark.unit
def test_template_global_registered(isolated_xdg: Path) -> None:
    """``csrf_token`` is a Jinja global producing a valid (anonymous) token."""
    assert "csrf_token" in templates.env.globals
    rendered = templates.env.from_string("{{ csrf_token() }}").render(request=None)
    assert csrf.validate_token(rendered) is True


@pytest.mark.unit
def test_token_is_bound_to_session(isolated_xdg: Path) -> None:
    """A token minted for one session is refused for another / for anon."""
    mine = csrf.issue_token("session-a")
    assert csrf.validate_token(mine, "session-a") is True
    assert csrf.validate_token(mine, "session-b") is False
    assert csrf.validate_token(mine, None) is False
    anon = csrf.issue_token()
    assert csrf.validate_token(anon, "session-a") is False
    assert "session-a" not in mine  # the id is digested, never embedded


@pytest.mark.unit
def test_post_without_token_rejected_when_enabled(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form POST with no token → 403 when auth is enabled."""
    _use_auth(monkeypatch, enabled=True)
    client = _wired_client()
    resp = client.post("/thing", data={"value": "x"})
    assert resp.status_code == 403


@pytest.mark.unit
def test_post_with_form_token_passes_when_enabled(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form POST carrying a template-issued token succeeds when enabled."""
    _use_auth(monkeypatch, enabled=True)
    token = csrf.issue_token()
    client = _wired_client()
    resp = client.post("/thing", data={"value": "x", "csrf_token": token})
    assert resp.status_code == 200
    assert resp.text == "ok:x"


@pytest.mark.unit
def test_post_with_header_token_passes_when_enabled(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token may also arrive via the X-CSRF-Token header (HTMX path)."""
    _use_auth(monkeypatch, enabled=True)
    token = csrf.issue_token()
    client = _wired_client()
    resp = client.post(
        "/thing",
        data={"value": "x"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.text == "ok:x"


@pytest.mark.unit
def test_post_without_token_passes_when_disabled(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSRF is a no-op when auth is disabled: POST passes without a token."""
    _use_auth(monkeypatch, enabled=False)
    client = _wired_client()
    resp = client.post("/thing", data={"value": "x"})
    assert resp.status_code == 200
    assert resp.text == "ok:x"


@pytest.mark.unit
def test_require_json_content_type_gate(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API content-type helper rejects non-JSON mutations when enabled."""
    app = FastAPI()

    @app.post("/api")
    async def _api(_ct: None = Depends(csrf.require_json_content_type)) -> PlainTextResponse:
        return PlainTextResponse("ok")

    _use_auth(monkeypatch, enabled=True)
    client = TestClient(app)

    form_resp = client.post("/api", data={"a": "b"})
    assert form_resp.status_code == 415

    json_resp = client.post("/api", json={"a": "b"})
    assert json_resp.status_code == 200

    _use_auth(monkeypatch, enabled=False)
    disabled_resp = client.post("/api", data={"a": "b"})
    assert disabled_resp.status_code == 200


@pytest.mark.unit
def test_settings_form_post_gated_end_to_end(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real /settings HTML route enforces CSRF when auth is enabled."""
    import importlib

    import ovid2.web.app as _app_module
    import ovid2.web.settings as _web_settings

    importlib.reload(_web_settings)
    importlib.reload(_app_module)

    _use_auth(monkeypatch, enabled=True)
    from ovid2.web import db as web_db
    from ovid2.web.auth.roles import Role
    from ovid2.web.services import sessions_svc, users_svc
    from tests.helpers.csrf import csrf_headers_for

    web_db.ensure_schema()
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject="csrf-admin",
        email="csrf-admin@example.org",
        name="csrf-admin",
    )
    users_svc.activate(user_id=record["user_id"], role=Role.SYSTEM_ADMIN, actor="test")
    session = sessions_svc.create(
        user_id=str(record["user_id"]), idp_sid=None, absolute_ttl_min=60, idle_ttl_min=45
    )
    with TestClient(_app_module.app) as client:
        from starlette.responses import Response

        response = Response()
        cookie_mod.issue_cookie(response, session.session_id)
        raw = next(v.decode("latin-1") for k, v in response.raw_headers if k == b"set-cookie")
        client.cookies.set("ovid2_session", raw.split(";", 1)[0].split("=", 1)[1])

        anonymous = TestClient(_app_module.app)
        assert (
            anonymous.post(
                "/settings", data={"defaults_engine": "pyspark"}, follow_redirects=False
            ).status_code
            == 401
        )

        no_token = client.post(
            "/settings", data={"defaults_engine": "pyspark"}, follow_redirects=False
        )
        assert no_token.status_code == 403

        # An anonymous (pre-auth) token is refused for a signed-in session.
        anon_token = csrf.issue_token()
        wrong = client.post(
            "/settings",
            data={"defaults_engine": "pyspark", "csrf_token": anon_token},
            follow_redirects=False,
        )
        assert wrong.status_code == 403

        with_token = client.post(
            "/settings",
            data={"defaults_engine": "pyspark"},
            headers=csrf_headers_for(client),
            follow_redirects=False,
        )
        assert with_token.status_code == 303


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "/pipelines",
        "/pipeline-packages/new",
        "/pipeline-packages/example_pcornet/formatters/new",
        "/pipeline-packages/example_pcornet/mappers/demographic/edit",
        "/pipelines/x/delete",
        "/pipelines/x/run",
    ],
)
def test_pipeline_mutation_routes_gated(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Pipeline / package / formatter / mapper form POSTs reject a missing token."""
    import importlib

    import ovid2.web.app as _app_module
    import ovid2.web.settings as _web_settings

    importlib.reload(_web_settings)
    importlib.reload(_app_module)
    _use_auth(monkeypatch, enabled=True)
    with TestClient(_app_module.app) as client:
        r = client.post(path, data={"name": "x"}, follow_redirects=False)
        # 403 (CSRF) or 401/303 (unauthenticated) — never a processed write.
        assert r.status_code in (401, 403) or (
            r.status_code == 303 and "/login" in r.headers.get("location", "")
        ), (path, r.status_code)
