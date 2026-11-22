"""Unit tests for the value-free secret status helpers and ``ovid2 tools secret-status``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ovid2.config.secrets import secret_fields_filled, secret_status


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XDG config at a temp dir; drop any scoped-secrets env."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("OVID2_SECRETS_DIR", "OVID2_SECRETS_FILE", "OVID2_SECRETS_SCOPE_MODE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write(name: str, data: dict[str, object], mode: int = 0o600) -> Path:
    secrets_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.unit
def test_fields_filled_detects_placeholders() -> None:
    data = {"account": "acme", "user": "REPLACE_ME", "password": "  "}
    assert secret_fields_filled(data, ("account", "user", "password")) == ["user", "password"]
    assert secret_fields_filled({"a": "x"}, ("a",)) == []
    assert secret_fields_filled({"a": 3}, ("a",)) == ["a"]


@pytest.mark.unit
def test_status_missing(isolated_xdg: Path) -> None:
    report = secret_status("nope", ("a",))
    assert report["exists"] is False and report["filled"] is False


@pytest.mark.unit
def test_status_never_contains_values(isolated_xdg: Path) -> None:
    _write("snow", {"account": "acct-123", "user": "u-456", "password": "pw-789"})
    report = secret_status("snow", ("account", "user", "password"))
    assert report["filled"] is True and report["mode_0600"] is True
    assert report["keys"] == ["account", "password", "user"]
    blob = json.dumps(report)
    for value in ("acct-123", "u-456", "pw-789"):
        assert value not in blob


@pytest.mark.unit
def test_status_world_readable_reports_error(isolated_xdg: Path) -> None:
    _write("loose", {"a": "x"}, mode=0o644)
    report = secret_status("loose", ("a",))
    assert report["exists"] is True
    assert report["error"] == "SecretPermissionError"
    assert report["filled"] is False


@pytest.mark.unit
def test_cli_secret_status_prints_value_free_json(isolated_xdg: Path) -> None:
    _write("snowflake", {"account": "acct-123", "user": "u", "password": "REPLACE_ME"})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ovid2",
            "tools",
            "secret-status",
            "--name",
            "snowflake",
            "--require",
            "account,user,password",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["filled"] is False and report["missing"] == ["password"]
    assert "acct-123" not in proc.stdout


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
