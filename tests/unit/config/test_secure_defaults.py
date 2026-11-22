"""Unit tests for the deployment-mode secure defaults (adr-secure-defaults).

* unset / production → auth on, secrets scope ``enforce``;
* ``OVID2_DEPLOYMENT=dev`` → auth off, scope ``warn``;
* an explicit ``auth.enabled`` / ``OVID2_SECRETS_SCOPE_MODE`` always wins;
* an unreadable ``settings.json`` fails closed (auth on) in production;
* ``ovid2 web`` refuses a network bind while auth is off;
* ``ovid2 init`` sets the break-glass password from the unattended file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from ovid2.config import deployment
from ovid2.config.secrets import scope_enforced, scope_mode


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "production"), ("", "production"), ("prod", "production"), ("DEV", "dev")],
)
def test_deployment_mode(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    else:
        monkeypatch.setenv(deployment.DEPLOYMENT_ENV, value)
    assert deployment.deployment_mode() == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.0.0.9", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("192.0.2.10", False),
        ("example.org", False),
    ],
)
def test_is_loopback_host(host: str, loopback: bool) -> None:
    assert deployment.is_loopback_host(host) is loopback


@pytest.mark.unit
def test_auth_default_follows_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.web.models import SettingsAuth

    monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    assert SettingsAuth().effective_enabled() is True
    monkeypatch.setenv(deployment.DEPLOYMENT_ENV, "dev")
    assert SettingsAuth().effective_enabled() is False
    # Explicit values always win.
    assert SettingsAuth(enabled=True).effective_enabled() is True
    monkeypatch.delenv(deployment.DEPLOYMENT_ENV)
    assert SettingsAuth(enabled=False).effective_enabled() is False


@pytest.mark.unit
def test_scope_mode_default_follows_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVID2_SECRETS_SCOPE_MODE", raising=False)
    monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    assert scope_mode() == "enforce" and scope_enforced()
    monkeypatch.setenv(deployment.DEPLOYMENT_ENV, "dev")
    assert scope_mode() == "warn"
    monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "enforce")
    assert scope_mode() == "enforce"
    monkeypatch.delenv(deployment.DEPLOYMENT_ENV)
    monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "warn")
    assert scope_mode() == "warn"
    monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "bogus")
    assert scope_mode() == "enforce"


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    settings_file = tmp_path / "config" / "ovid2" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    monkeypatch.setenv("OVID2_SETTINGS_FILE", str(settings_file))
    monkeypatch.setenv("OVID2_SCHEDULER_DB", str(tmp_path / "state" / "scheduler.db"))
    return settings_file


@pytest.mark.unit
def test_corrupt_settings_fail_closed(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.web.services import settings_svc

    xdg.write_text('{"auth": {"enabled": false', encoding="utf-8")  # truncated JSON
    monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    settings_svc._CACHE["mtime"] = None
    assert settings_svc.get_settings().auth.effective_enabled() is True


@pytest.mark.unit
def test_web_refuses_network_bind_without_auth(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from ovid2.web import cli as web_cli
    from ovid2.web.services import settings_svc

    xdg.write_text(json.dumps({"auth": {"enabled": False}}), encoding="utf-8")
    settings_svc._CACHE["mtime"] = None
    assert web_cli.network_bind_refusal("127.0.0.1") is None
    assert web_cli.network_bind_refusal("192.0.2.10")  # refused
    assert web_cli.network_bind_refusal("0.0.0.0")  # refused
    assert web_cli.network_bind_refusal("192.0.2.10", insecure=True) is None
    xdg.write_text(json.dumps({"auth": {"enabled": True}}), encoding="utf-8")
    settings_svc._CACHE["mtime"] = None
    assert web_cli.network_bind_refusal("192.0.2.10") is None


@pytest.mark.unit
def test_web_main_exits_before_serving(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("uvicorn")
    import uvicorn

    from ovid2.web import cli as web_cli
    from ovid2.web.services import settings_svc

    xdg.write_text(json.dumps({"auth": {"enabled": False}}), encoding="utf-8")
    settings_svc._CACHE["mtime"] = None
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))
    args = argparse.Namespace(
        bind="192.0.2.10:8100", reload=False, log_level="info", insecure_network=False
    )
    assert web_cli.main(args) == 2


@pytest.mark.unit
def test_mcp_http_refuses_network_bind() -> None:
    """Run in a subprocess: importing ``ovid2.mcp.server`` here would pin a
    ``WebApiError`` class that ``test_client_auth``'s module reload replaces."""
    pytest.importorskip("mcp")
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "ovid2", "mcp", "--transport", "http", "--bind", "192.0.2.10:8101"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert "refusing to serve HTTP" in proc.stderr


@pytest.mark.unit
def test_init_sets_breakglass_from_file(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("argon2")
    from ovid2.installer import init
    from ovid2.web.auth import breakglass

    monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    pw = tmp_path / "bg.txt"
    pw.write_text("a-long-enough-pass\n", encoding="utf-8")
    os.chmod(pw, 0o600)
    monkeypatch.setenv(init.BREAKGLASS_PASSWORD_FILE_ENV, str(pw))
    log = init.ensure_breakglass(interactive=False)
    assert any("set break-glass" in line for line in log), log
    assert breakglass.is_configured()
    assert breakglass.verify("a-long-enough-pass")
    # Second run keeps it.
    assert init.ensure_breakglass(interactive=False) == ["kept existing break-glass admin password"]


@pytest.mark.unit
def test_init_breakglass_hint_and_dev_skip(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("argon2")
    from ovid2.installer import init

    monkeypatch.delenv(init.BREAKGLASS_PASSWORD_FILE_ENV, raising=False)
    monkeypatch.delenv(deployment.DEPLOYMENT_ENV, raising=False)
    log = init.ensure_breakglass(interactive=False)
    assert log and "reset-break-glass" in log[0]
    monkeypatch.setenv(deployment.DEPLOYMENT_ENV, "dev")
    assert init.ensure_breakglass(interactive=False) == []


@pytest.mark.unit
def test_dev_instance_env_marks_dev_and_loopback_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.cli import dev

    monkeypatch.delenv("OVID2_DEV_HOST", raising=False)
    assert dev.default_host() == "127.0.0.1"
    env = dev.instance_env(
        Path("/tmp/x"), name="n", host="127.0.0.1", port=1, mcp_port=2, image="i", base={}
    )
    assert env["OVID2_DEPLOYMENT"] == "dev"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
