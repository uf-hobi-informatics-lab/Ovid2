"""Tests for the global-settings service (W7)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.models import GlobalSettings, SettingsDefaults
from ovid2.web.services import settings_svc


@pytest.mark.unit
def test_defaults_when_no_file(isolated_xdg: Path) -> None:
    settings_svc.invalidate_cache()
    settings = settings_svc.get_settings()
    assert settings.defaults.engine == "pyspark"
    assert settings.worker.route_dispatch_enabled is True


@pytest.mark.unit
def test_save_and_reload(isolated_xdg: Path) -> None:
    settings_svc.invalidate_cache()
    payload = GlobalSettings(defaults=SettingsDefaults(engine="test-eng", workers=42))
    settings_svc.save_settings(payload, actor="test")
    settings_svc.invalidate_cache()
    settings = settings_svc.get_settings()
    assert settings.defaults.engine == "test-eng"
    assert settings.defaults.workers == 42


@pytest.mark.unit
def test_cache_is_mtime_aware(isolated_xdg: Path) -> None:
    settings_svc.invalidate_cache()
    settings_svc.save_settings(GlobalSettings(defaults=SettingsDefaults(engine="v1")), actor="test")
    first = settings_svc.get_settings()
    assert first.defaults.engine == "v1"
    # Overwrite the file on disk (bumping mtime) — cache must invalidate.
    path = settings_svc.settings_path()
    # Bump ns to make sure the mtime differs from the initial write.
    os.utime(path, ns=(0, 1))
    payload = json.loads(path.read_text())
    payload["defaults"]["engine"] = "v2"
    path.write_text(json.dumps(payload))
    second = settings_svc.get_settings()
    assert second.defaults.engine == "v2"


@pytest.mark.unit
def test_bad_file_falls_back_to_defaults(isolated_xdg: Path) -> None:
    settings_svc.invalidate_cache()
    path = settings_svc.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not JSON")
    settings = settings_svc.get_settings()
    # Bad file must not crash — we get the built-in defaults instead.
    assert settings.defaults.engine == "pyspark"
