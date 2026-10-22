"""Fixtures for dataspace tests.

Provides an ``isolated_xdg`` fixture that is a superset of the web-tier
version — it additionally redirects the working directory into
``tmp_path`` so ``./configs`` (which sits on the address-loader search
path) cannot leak the repo's real address configs into a test.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """XDG + cwd isolation for dataspace tests."""
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path / "runtime" / "ovid2"))
    monkeypatch.setenv(
        "OVID2_SCHEDULER_DB", str(tmp_path / "state" / "ovid2" / "scheduler.db")
    )
    monkeypatch.setenv(
        "OVID2_CONFIGS_DIR", str(tmp_path / "config" / "ovid2" / "configs")
    )
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR",
        str(tmp_path / "config" / "ovid2" / "connections"),
    )
    monkeypatch.setenv(
        "OVID2_SETTINGS_FILE",
        str(tmp_path / "config" / "ovid2" / "settings.json"),
    )
    (tmp_path / "config" / "ovid2" / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ovid2" / "connections").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ovid2").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "ovid2").mkdir(parents=True, exist_ok=True)

    import ovid2.config.settings as _settings

    importlib.reload(_settings)
    # Bust the settings_svc cache so previous tests don't leak settings.
    try:
        from ovid2.web.services import settings_svc

        settings_svc.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass
    yield tmp_path
