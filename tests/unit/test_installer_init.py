"""Unit tests for ``ovid2 init`` bootstrap logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.installer import init as init_mod


@pytest.mark.unit
def test_bootstrap_creates_full_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bootstrap` should create every XDG directory and drop the templates."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    log = init_mod.bootstrap(interactive=False)

    ovid_root = tmp_path / "config" / "ovid2"
    assert (ovid_root / "configs").is_dir()
    assert (ovid_root / "secrets.d").is_dir()

    secret = ovid_root / "secrets.d" / "snowflake.json"
    assert secret.is_file()
    mode = secret.stat().st_mode & 0o777
    assert mode == 0o600, f"snowflake.json must be 0600, got {oct(mode)}"

    # Log mentions every directory action
    joined = "\n".join(log)
    assert "config dir" in joined
    assert "secrets dir" in joined


@pytest.mark.unit
def test_bootstrap_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running bootstrap twice should not clobber existing files."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    init_mod.bootstrap(interactive=False)
    secret = tmp_path / "config" / "ovid2" / "secrets.d" / "snowflake.json"
    secret.write_text(json.dumps({"account": "custom", "user": "me"}))
    secret.chmod(0o600)

    init_mod.bootstrap(interactive=False)

    data = json.loads(secret.read_text())
    assert data["user"] == "me", "existing secret must not be clobbered"


@pytest.mark.unit
def test_snowflake_template_has_expected_keys() -> None:
    """The default Snowflake template covers every required field."""
    assert set(init_mod._SNOWFLAKE_TEMPLATE) >= {
        "account",
        "user",
        "password",
        "role",
        "warehouse",
        "database",
        "schema",
    }
