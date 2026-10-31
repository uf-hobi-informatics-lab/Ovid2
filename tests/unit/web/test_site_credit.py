"""Attribution credit is present (and quiet) on every page shell.

The ``_credit.html`` partial is included by ``base.html`` (all shell pages)
and by the standalone ``login.html`` / ``pending.html`` documents. These
tests pin that wiring and that the styling only uses existing theme tokens
so it re-themes with every theme switch.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ovid2.web import db as web_db
from ovid2.web.app import create_app
from ovid2.web.auth import cookie as cookie_mod
from ovid2.web.models import GlobalSettings, SettingsAuth
from ovid2.web.services import settings_svc
from ovid2.web.templating import templates

pytestmark = pytest.mark.unit

_WEB = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web"
_CREDIT = "Developed and maintained by"


@pytest.fixture(autouse=True)
def _temp_db_and_key(isolated_xdg: Path) -> None:
    """Provision a clean DB and reset the cookie signing-key cache."""
    web_db.ensure_schema()
    cookie_mod.reset_signing_key_cache()


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An auth-disabled ``TestClient``."""
    settings = GlobalSettings(auth=SettingsAuth(enabled=False))
    monkeypatch.setattr(settings_svc, "get_settings", lambda: settings)
    client = TestClient(create_app(), follow_redirects=False)
    try:
        yield client
    finally:
        client.close()


def test_shell_footer_carries_credit(app_client: TestClient) -> None:
    """Every base-shell page shows the credit inside the site footer."""
    body = app_client.get("/").text
    footer = body[body.index('<footer class="site-footer"') :]
    footer = footer[: footer.index("</footer>")]
    assert 'class="site-credit"' in footer
    assert _CREDIT in footer
    assert 'class="site-credit-org"' in footer
    # The "No auth" reminder must stay distinct from the credit.
    assert "No auth" not in footer[footer.index('class="site-credit"') :]


@pytest.mark.parametrize("name", ["login.html", "pending.html"])
def test_standalone_pages_carry_credit(name: str) -> None:
    """Login and pending pages (which skip base.html) also show the credit."""
    tmpl = templates.env.get_template(name)
    html = tmpl.render(
        request=None,
        providers=[],
        breakglass_configured=False,
        next="/",
        error=None,
        no_admin_configured=False,
        email=None,
    )
    assert 'class="site-credit-standalone"' in html
    assert _CREDIT in html


def test_credit_css_uses_only_theme_tokens() -> None:
    """The base ``.site-credit`` rule colours itself via theme variables only."""
    css = (_WEB / "static" / "ovid2.css").read_text(encoding="utf-8")
    m = re.search(r"\n\.site-credit \{(?P<body>[^}]*)\}", css)
    assert m, ".site-credit rule missing"
    color = re.search(r"\bcolor:\s*([^;]+);", m.group("body"))
    assert color and color.group(1).strip().startswith("var(--"), "credit colour must be a token"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", m.group("body"))


if __name__ == "__main__":
    raise SystemExit("test module; run via pytest.")
