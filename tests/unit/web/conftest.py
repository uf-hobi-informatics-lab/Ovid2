"""Shared fixtures for web-tier tests.

Isolates every test into its own XDG tree so the SQLite ``scheduler.db``,
per-run event logs, and address configs cannot collide with the host's
real Ovid2 install.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every XDG env var into ``tmp_path``.

    Yields the root ``tmp_path`` so tests can inspect the tree.
    """
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))
    # Force reload of ovid2.config.settings.WORK_DIR — it snapshots XDG at
    # import time.
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path / "runtime" / "ovid2"))
    monkeypatch.setenv("OVID2_SCHEDULER_DB", str(tmp_path / "state" / "ovid2" / "scheduler.db"))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(tmp_path / "config" / "ovid2" / "configs"))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(tmp_path / "config" / "ovid2" / "connections"))
    monkeypatch.setenv("OVID2_ROUTES_DIR", str(tmp_path / "config" / "ovid2" / "routes"))
    monkeypatch.setenv("OVID2_ENGINES_DIR", str(tmp_path / "config" / "ovid2" / "engines"))
    monkeypatch.setenv("OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json"))
    (tmp_path / "config" / "ovid2" / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ovid2" / "connections").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ovid2" / "routes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ovid2" / "engines").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ovid2").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "ovid2").mkdir(parents=True, exist_ok=True)

    # Reload the settings module so WORK_DIR picks up the new env vars.
    import importlib

    import ovid2.config.settings as _settings

    importlib.reload(_settings)
    yield tmp_path


@pytest.fixture()
def web_client(isolated_xdg: Path) -> Iterator[object]:
    """Return a ``TestClient`` bound to a freshly-created FastAPI app."""
    pytest.importorskip("fastapi")
    # Reload web modules to pick up patched env vars.
    import importlib

    from fastapi.testclient import TestClient

    import ovid2.web.app as _app_module
    import ovid2.web.settings as _web_settings

    importlib.reload(_web_settings)
    importlib.reload(_app_module)
    with TestClient(_app_module.app) as client:
        yield client


@pytest.fixture()
def sample_address(isolated_xdg: Path) -> Path:
    """Drop a sample disc address config on the search path."""
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    path = configs / "test_disc.json"
    path.write_text(
        '{"src_name": "disc", "src_args": {"path": "/tmp/example"}}',
        encoding="utf-8",
    )
    return path
