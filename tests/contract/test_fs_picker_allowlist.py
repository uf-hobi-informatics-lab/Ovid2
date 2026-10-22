"""Contract: the fs picker refuses paths outside the allowlist.

The picker is the operator's window into the host filesystem from the
web UI. The allowlist (``settings.fs_picker.roots``) is the sole
guardrail; a regression that bypassed it would turn the picker into a
filesystem-recon tool. This test freezes the invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.contract
def test_fs_picker_rejects_outside_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two sibling directories; only one is in the allowlist.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    cfg = tmp_path / "config" / "ovid2"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(
        json.dumps({"fs_picker": {"enabled": True, "roots": [str(allowed)]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OVID2_SETTINGS_FILE", str(cfg / "settings.json"))

    from ovid2.web.services import fs_svc, settings_svc
    settings_svc.invalidate_cache()

    # In-allowlist path succeeds.
    listing = fs_svc.list_dir(str(allowed))
    assert listing.path == str(allowed)

    # Sibling path fails.
    with pytest.raises(fs_svc.FsAccessError):
        fs_svc.list_dir(str(outside))

    # `..` traversal that lands outside the allowlist also fails.
    with pytest.raises(fs_svc.FsAccessError):
        fs_svc.list_dir(f"{allowed}/../outside")


@pytest.mark.contract
def test_fs_picker_disable_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When enabled=False, list_dir raises even for the allowlist root itself."""
    cfg = tmp_path / "config" / "ovid2"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(
        json.dumps({"fs_picker": {"enabled": False, "roots": ["/"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OVID2_SETTINGS_FILE", str(cfg / "settings.json"))
    from ovid2.web.services import fs_svc, settings_svc
    settings_svc.invalidate_cache()

    with pytest.raises(fs_svc.FsPickerDisabledError):
        fs_svc.list_dir("/")
