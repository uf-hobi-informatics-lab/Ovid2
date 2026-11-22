"""Unit tests for the animated ``<ovid-mark>`` logo surfaces.

Covers the dev-only demo route, the login / pending marks and the static
assets the header loads.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_dev_demo_404_outside_dev(web_client: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/dev/ovid-mark`` never ships as a public surface.

    Sign-in is forced off so the 404 comes from the deployment gate, not
    from the auth redirect.
    """
    from ovid2.web.models import GlobalSettings, SettingsAuth
    from ovid2.web.services import settings_svc

    settings = GlobalSettings(auth=SettingsAuth(enabled=False))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    for mode in (None, "production"):
        if mode is None:
            monkeypatch.delenv("OVID2_DEPLOYMENT", raising=False)
        else:
            monkeypatch.setenv("OVID2_DEPLOYMENT", mode)
        r = web_client.get("/dev/ovid-mark", follow_redirects=False)  # type: ignore[attr-defined]
        assert r.status_code == 404, (mode, r.status_code)


@pytest.mark.unit
def test_dev_demo_renders_in_dev(web_client: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """In dev the demo shows a button per state and the four sizes."""
    monkeypatch.setenv("OVID2_DEPLOYMENT", "dev")
    r = web_client.get("/dev/ovid-mark")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.text
    for state in (
        "idle",
        "extract",
        "transform",
        "load",
        "running",
        "sync",
        "success",
        "warning",
        "error",
    ):
        assert f'data-ovm-state="{state}"' in body, state
    for px in (300, 96, 48, 24):
        assert f'size="{px}"' in body, px
    assert "/static/ovid_mark_demo.js" in body
    assert "data-ovm-autoplay" in body


@pytest.mark.unit
def test_dev_demo_excluded_from_openapi(web_client: object) -> None:
    """The demo is an HTML surface, not API."""
    paths = web_client.get("/openapi.json").json().get("paths", {})  # type: ignore[attr-defined]
    assert "/dev/ovid-mark" not in paths


@pytest.mark.unit
def test_login_page_uses_the_mark() -> None:
    """The sign-in card shows the 64 px sprout instead of the raster PNG."""
    body = _template_text("login.html")
    assert '<ovid-mark class="login-mark" size="64"' in body
    assert "/static/ovid_mark.js" in body
    assert "ovid_logo_128.png" not in body


@pytest.mark.unit
@pytest.mark.parametrize("name", ["ovid_mark.js", "ovid_mark_status.js", "ovid_mark_demo.js"])
def test_mark_assets_served(web_client: object, name: str) -> None:
    """The component, status driver and demo script are served from /static."""
    r = web_client.get(f"/static/{name}")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert len(r.content) > 500


@pytest.mark.unit
def test_mark_colour_defaults_have_zero_specificity(web_client: object) -> None:
    """``:root`` mark defaults use ``:where`` so a theme block always wins."""
    css = web_client.get("/static/ovid2.css").text  # type: ignore[attr-defined]
    assert ":where(:root) {\n  --ovid-base:" in css
    assert "--ovid-fav-running:" in css


def _template_text(name: str) -> str:
    from pathlib import Path

    import ovid2.web as web

    return (Path(web.__file__).parent / "templates" / name).read_text(encoding="utf-8")


@pytest.mark.unit
def test_pending_page_uses_the_mark() -> None:
    """The awaiting-approval card shows the same 64 px sprout."""
    body = _template_text("pending.html")
    assert '<ovid-mark class="login-mark" size="64"' in body
    assert "/static/ovid_mark.js" in body
