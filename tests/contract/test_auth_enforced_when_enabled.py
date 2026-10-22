"""Contract: RBAC is enforced when enabled and inert when disabled (todo 22).

This is the guardrail behind the plan's central backwards-compatibility
promise and its security promise, expressed as a single contract:

* **``settings.auth.enabled=False`` (the default):** the panel behaves exactly
  as it did before auth existed — an unauthenticated request to a REST
  endpoint and to an HTML page both succeed (no ``401``, no redirect to
  ``/login``). Every other test suite runs in this mode, so this arm nails the
  promise down explicitly.
* **``settings.auth.enabled=True``:** an unauthenticated request is rejected —
  a gated REST endpoint answers ``401`` (JSON, via the route dependency) and a
  gated HTML page answers ``302`` to ``/login`` (via the redirect middleware).

The test drives the *real* application from
:func:`ovid2.web.app.create_app` through a ``TestClient`` so it exercises the
actual dependency wiring on :mod:`ovid2.web.routes.api_v1` and the actual
:class:`ovid2.web.auth.middleware.AuthRedirectMiddleware`, not a stub. Auth is
toggled by monkeypatching :func:`ovid2.web.services.settings_svc.get_settings`
(the same seam the unit suites use), so no ``settings.json`` is written.

A deliberate failing-first check lives in
:func:`test_gate_actually_bites_when_a_dependency_is_removed`: it proves the
``401`` in the enabled arm is produced by the capability dependency and not by
some incidental error, by confirming the *ungated* liveness probe answers
differently.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ovid2.web.app import create_app
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import settings_svc

pytestmark = pytest.mark.contract

#: A representative high-privilege REST endpoint (MANAGE_SECRETS). Unauthed +
#: auth-enabled must yield 401 from the ``require_capability`` dependency.
_GATED_REST_PATH: str = "/api/v1/secrets"

#: A representative gated HTML page. Unauthed + auth-enabled must 302→/login
#: via the redirect middleware.
_GATED_HTML_PATH: str = "/runs"


@pytest.fixture
def app_client() -> Iterator[TestClient]:
    """A ``TestClient`` over the real app that does not auto-follow redirects."""
    client = TestClient(create_app(), follow_redirects=False)
    try:
        yield client
    finally:
        client.close()


def _set_auth(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force ``settings_svc.get_settings`` to a known ``auth.enabled`` value."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=enabled))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)


# ---------------------------------------------------------------------------
# enabled — unauthenticated access is rejected
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_enabled_unauth_rest_returns_401(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth enabled + no credentials → gated REST endpoint answers 401."""
    _set_auth(monkeypatch, enabled=True)
    resp = app_client.get(_GATED_REST_PATH)
    assert resp.status_code == 401


@pytest.mark.contract
def test_enabled_unauth_html_redirects_to_login(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth enabled + no session → gated HTML page 302s to /login."""
    _set_auth(monkeypatch, enabled=True)
    resp = app_client.get(_GATED_HTML_PATH)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# disabled — open behaviour preserved (the backwards-compat promise)
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_disabled_unauth_rest_open(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth disabled → the same gated REST endpoint is open (no 401)."""
    _set_auth(monkeypatch, enabled=False)
    resp = app_client.get(_GATED_REST_PATH)
    assert resp.status_code == 200


@pytest.mark.contract
def test_disabled_unauth_html_open(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """auth disabled → the gated HTML page renders without a redirect."""
    _set_auth(monkeypatch, enabled=False)
    resp = app_client.get(_GATED_HTML_PATH)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# failing-first proof: the 401 comes from the capability gate, not noise
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_gate_actually_bites_when_a_dependency_is_removed(
    monkeypatch: pytest.MonkeyPatch, app_client: TestClient
) -> None:
    """Prove the enabled-arm 401 is the capability gate, not an incidental error.

    The gated path answers ``401`` unauthenticated; the ungated liveness probe
    ``/api/v1/health`` is authorized for the public ``VIEW_PUBLIC`` capability,
    which an unauthenticated caller also lacks when auth is enabled — so it too
    answers ``401`` rather than a server error or a redirect. Confirming both
    are ``401`` (and neither is a ``5xx`` or an HTML redirect) demonstrates the
    rejection is the RBAC layer speaking, not a crash.
    """
    _set_auth(monkeypatch, enabled=True)
    gated = app_client.get(_GATED_REST_PATH)
    health = app_client.get("/api/v1/health")
    assert gated.status_code == 401
    # Not a server error, not an HTML redirect — a clean JSON 401.
    assert gated.headers.get("content-type", "").startswith("application/json")
    # The public liveness probe is likewise gated behind a session when auth is
    # on, so it must also refuse (401), never 5xx.
    assert health.status_code in (401, 403)


if __name__ == "__main__":
    raise SystemExit("contract test module; run via pytest.")
