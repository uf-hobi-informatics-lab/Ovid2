"""REST capability-gating tests (Wave 3, todo 14).

Exercises the ``Depends(require_capability(...))`` gates attached to every
``/api/v1`` route in :mod:`ovid2.web.routes.api_v1`.

Strategy
--------
Auth is forced on via a ``settings_svc.get_settings`` patch (mirrors
``test_auth_deps.py``); a real session + user row is created through
``sessions_svc`` / ``users_svc`` and threaded into the ``TestClient`` as a
signed ``ovid2_session`` cookie. Each representative endpoint (one per
capability tier) is then driven with viewer / operator / admin / guest /
pending contexts and asserted to answer 200/403/401 correctly.

A final pair of tests confirms that with ``settings.auth.enabled=False`` a
representative gated endpoint behaves exactly as it does today (fully open).
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


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` over a freshly constructed app."""
    client = TestClient(create_app())
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


def _pending_cookie(subject: str) -> str:
    """Create a pending user (default state) + session; return its cookie."""
    record = users_svc.upsert_from_idp(
        issuer="https://accounts.google.com",
        subject=subject,
        email=f"{subject}@example.org",
        name=subject,
    )
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


def _get(client: TestClient, path: str, cookie: str | None) -> int:
    """GET ``path`` with an optional signed session cookie; return status."""
    _set_cookie(client, cookie)
    return client.get(path).status_code


def _post(client: TestClient, path: str, cookie: str | None) -> int:
    """POST ``path`` with an optional signed session cookie; return status."""
    _set_cookie(client, cookie)
    return client.post(path, json={}).status_code


def _put(client: TestClient, path: str, cookie: str | None) -> int:
    """PUT ``path`` with an optional signed session cookie; return status."""
    _set_cookie(client, cookie)
    return client.put(path, json={}).status_code


# ---------------------------------------------------------------------------
# VIEW_PUBLIC tier — /health, /cdms (open to guest; 401 unauth)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_health_view_public_guest_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A guest can hit the VIEW_PUBLIC liveness endpoint."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.GUEST, "guest-health")
    assert _get(app_client, "/api/v1/health", cookie) == 200


@pytest.mark.unit
def test_health_view_public_unauth_401(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """No session → 401 even on the VIEW_PUBLIC liveness endpoint."""
    _enable_auth(monkeypatch, enabled=True)
    assert _get(app_client, "/api/v1/health", None) == 401


@pytest.mark.unit
def test_cdms_view_public_guest_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A guest can list CDMs (VIEW_PUBLIC)."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.GUEST, "guest-cdms")
    assert _get(app_client, "/api/v1/cdms", cookie) == 200


# ---------------------------------------------------------------------------
# VIEW_RUNS tier — /runs (viewer allowed; guest 403; pending 403; unauth 401)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runs_view_runs_viewer_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A viewer can list runs (VIEW_RUNS)."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.VIEWER, "viewer-runs")
    assert _get(app_client, "/api/v1/runs", cookie) == 200


@pytest.mark.unit
def test_runs_view_runs_guest_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A guest lacks VIEW_RUNS → 403 on /runs."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.GUEST, "guest-runs")
    assert _get(app_client, "/api/v1/runs", cookie) == 403


@pytest.mark.unit
def test_runs_view_runs_pending_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A pending user is authenticated but has no caps → 403 (not 401)."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _pending_cookie("pending-runs")
    assert _get(app_client, "/api/v1/runs", cookie) == 403


@pytest.mark.unit
def test_runs_view_runs_unauth_401(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """No session → 401 on /runs."""
    _enable_auth(monkeypatch, enabled=True)
    assert _get(app_client, "/api/v1/runs", None) == 401


# ---------------------------------------------------------------------------
# VIEW_PIPELINE_DATA tier — /pipelines (operator allowed; viewer 403)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipelines_view_pipeline_data_operator_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A pipeline operator can list pipelines (VIEW_PIPELINE_DATA)."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-pipelines")
    assert _get(app_client, "/api/v1/pipelines", cookie) == 200


@pytest.mark.unit
def test_pipelines_view_pipeline_data_viewer_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A viewer lacks VIEW_PIPELINE_DATA → 403 on /pipelines."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.VIEWER, "viewer-pipelines")
    assert _get(app_client, "/api/v1/pipelines", cookie) == 403


# ---------------------------------------------------------------------------
# SUBMIT_JOBS tier — POST /runs (operator allowed; viewer 403)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_run_submit_jobs_operator_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An operator passes the SUBMIT_JOBS gate on POST /runs.

    The empty JSON body fails request validation (422) — that is *past* the
    capability gate, which is exactly what this test asserts (not 401/403).
    """
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-submit")
    status = _post(app_client, "/api/v1/runs", cookie)
    assert status not in (401, 403)


@pytest.mark.unit
def test_submit_run_submit_jobs_viewer_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A viewer lacks SUBMIT_JOBS → 403 on POST /runs."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.VIEWER, "viewer-submit")
    assert _post(app_client, "/api/v1/runs", cookie) == 403


# ---------------------------------------------------------------------------
# MANAGE_CONFIG tier — PUT /addresses/{name} (admin allowed; operator 403)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_address_write_manage_config_operator_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An operator lacks MANAGE_CONFIG → 403 on PUT /addresses/{name}."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-addr")
    assert _put(app_client, "/api/v1/addresses/x", cookie) == 403


@pytest.mark.unit
def test_address_write_manage_config_admin_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An admin passes the MANAGE_CONFIG gate on PUT /addresses/{name}."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-addr")
    assert _put(app_client, "/api/v1/addresses/x", cookie) not in (401, 403)


# ---------------------------------------------------------------------------
# MANAGE_SECRETS tier — GET /secrets (admin allowed; operator 403)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_secrets_manage_secrets_operator_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An operator lacks MANAGE_SECRETS → 403 on GET /secrets."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-secrets")
    assert _get(app_client, "/api/v1/secrets", cookie) == 403


@pytest.mark.unit
def test_secrets_manage_secrets_admin_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An admin can list secrets (MANAGE_SECRETS)."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-secrets")
    assert _get(app_client, "/api/v1/secrets", cookie) == 200


@pytest.mark.unit
def test_secrets_write_viewer_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """The plan's failure scenario: viewer POSTs a secret → 403."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.VIEWER, "viewer-secret-post")
    assert _post(app_client, "/api/v1/secrets/x", cookie) == 403


# ---------------------------------------------------------------------------
# ADMIN_ALL tier — GET /install/status, PUT /settings, /approvals
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_status_admin_all_operator_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An operator lacks ADMIN_ALL → 403 on GET /install/status."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-install")
    assert _get(app_client, "/api/v1/install/status", cookie) == 403


@pytest.mark.unit
def test_install_status_admin_all_admin_allowed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An admin passes the ADMIN_ALL gate on GET /install/status."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.SYSTEM_ADMIN, "admin-install")
    assert _get(app_client, "/api/v1/install/status", cookie) not in (401, 403)


@pytest.mark.unit
def test_approvals_admin_all_viewer_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """A viewer lacks ADMIN_ALL → 403 on GET /approvals."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.VIEWER, "viewer-approvals")
    assert _get(app_client, "/api/v1/approvals", cookie) == 403


@pytest.mark.unit
def test_settings_put_admin_all_operator_forbidden(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """An operator lacks ADMIN_ALL → 403 on PUT /settings."""
    _enable_auth(monkeypatch, enabled=True)
    cookie = _cookie_for_role(Role.PIPELINE_OPERATOR, "op-settings")
    assert _put(app_client, "/api/v1/settings", cookie) == 403


# ---------------------------------------------------------------------------
# auth disabled → open access, byte-identical to today
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_auth_secrets_open(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth.enabled=False → the MANAGE_SECRETS endpoint is open (no cookie)."""
    _enable_auth(monkeypatch, enabled=False)
    assert _get(app_client, "/api/v1/secrets", None) == 200


@pytest.mark.unit
def test_disabled_auth_runs_open(monkeypatch: pytest.MonkeyPatch, app_client: TestClient) -> None:
    """auth.enabled=False → the VIEW_RUNS endpoint is open (no cookie)."""
    _enable_auth(monkeypatch, enabled=False)
    assert _get(app_client, "/api/v1/runs", None) == 200


@pytest.mark.unit
def test_disabled_auth_settings_put_open(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth.enabled=False → the ADMIN_ALL PUT endpoint passes the gate."""
    _enable_auth(monkeypatch, enabled=False)
    assert _put(app_client, "/api/v1/settings", None) not in (401, 403)


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
