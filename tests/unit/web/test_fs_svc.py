"""Unit tests for :mod:`ovid2.web.services.fs_svc`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class TestNormaliseAllowlist:
    def test_absolute_paths_are_kept(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist(["/data", "/processing"]) == ["/data", "/processing"]

    def test_relative_paths_dropped(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist(["relative", "/keep"]) == ["/keep"]

    def test_deduplicated_preserving_order(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist(["/a", "/b", "/a"]) == ["/a", "/b"]

    def test_trailing_slashes_normalised(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist(["/a/"]) == ["/a"]

    def test_fallback_when_empty(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist([]) == ["/"]

    def test_fallback_when_only_bad_entries(self) -> None:
        from ovid2.web.services.fs_svc import normalise_allowlist

        assert normalise_allowlist(["relative", "", None]) == ["/"]  # type: ignore[list-item]


def _configure_settings(tmp_path: Path, roots: list[str], enabled: bool = True) -> None:
    """Write a settings.json with a specific fs_picker allowlist."""
    cfg_dir = tmp_path / "config" / "ovid2"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"fs_picker": {"enabled": enabled, "roots": roots}}),
        encoding="utf-8",
    )
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    os.environ["OVID2_SETTINGS_FILE"] = str(cfg_dir / "settings.json")
    from ovid2.web.services import settings_svc

    settings_svc.invalidate_cache()


class TestListDir:
    def test_lists_a_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        target = tmp_path / "target"
        target.mkdir()
        _configure_settings(tmp_path, [str(target)])
        (target / "sub").mkdir()
        (target / "file.txt").write_text("hi", encoding="utf-8")

        from ovid2.web.services import fs_svc

        listing = fs_svc.list_dir(str(target))
        names = [e.name for e in listing.entries]
        assert "sub" in names
        assert "file.txt" in names
        # Directories sort first.
        assert listing.entries[0].name == "sub"
        assert listing.entries[0].type == "dir"

    def test_rejects_relative_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        _configure_settings(tmp_path, ["/"])

        from ovid2.web.services import fs_svc

        with pytest.raises(fs_svc.FsAccessError):
            fs_svc.list_dir("relative/path")

    def test_rejects_path_outside_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        _configure_settings(tmp_path, [str(allowed)])

        outside = tmp_path / "outside"
        outside.mkdir()

        from ovid2.web.services import fs_svc

        with pytest.raises(fs_svc.FsAccessError):
            fs_svc.list_dir(str(outside))

    def test_rejects_dotdot_traversal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (allowed / "sub").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _configure_settings(tmp_path, [str(allowed)])

        from ovid2.web.services import fs_svc

        # realpath resolves ..  → tmp_path/outside → outside allowlist.
        with pytest.raises(fs_svc.FsAccessError):
            fs_svc.list_dir(f"{allowed}/../outside")

    def test_missing_path_raises_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        _configure_settings(tmp_path, [str(tmp_path)])

        from ovid2.web.services import fs_svc

        with pytest.raises(fs_svc.FsNotFoundError):
            fs_svc.list_dir(str(tmp_path / "nope"))

    def test_disabled_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        _configure_settings(tmp_path, ["/"], enabled=False)

        from ovid2.web.services import fs_svc

        with pytest.raises(fs_svc.FsPickerDisabledError):
            fs_svc.list_dir("/")

    def test_hidden_files_hidden_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        target = tmp_path / "target"
        target.mkdir()
        _configure_settings(tmp_path, [str(target)])
        (target / "visible").mkdir()
        (target / ".hidden").mkdir()

        from ovid2.web.services import fs_svc

        listing = fs_svc.list_dir(str(target))
        names = [e.name for e in listing.entries]
        assert "visible" in names
        assert ".hidden" not in names

        listing2 = fs_svc.list_dir(str(target), show_hidden=True)
        names2 = [e.name for e in listing2.entries]
        assert ".hidden" in names2

    def test_parent_is_none_at_allowlist_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        _configure_settings(tmp_path, [str(allowed)])

        from ovid2.web.services import fs_svc

        listing = fs_svc.list_dir(str(allowed))
        # parent would be tmp_path which is outside the allowlist.
        assert listing.parent is None


class TestListRoots:
    def test_returns_curated_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _configure_settings(tmp_path, ["/data", "/processing"])

        from ovid2.web.services import fs_svc

        result = fs_svc.list_roots()
        assert result.enabled is True
        paths = [r.path for r in result.roots]
        assert str(home) in paths
        assert "/data" in paths
        assert "/processing" in paths
        assert "/" in paths  # last-resort catch-all

    def test_deduplicated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        _configure_settings(tmp_path, ["/", "/"])
        from ovid2.web.services import fs_svc

        result = fs_svc.list_roots()
        paths = [r.path for r in result.roots]
        assert paths.count("/") == 1

    def test_disabled_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        _configure_settings(tmp_path, ["/"], enabled=False)
        from ovid2.web.services import fs_svc

        result = fs_svc.list_roots()
        assert result.enabled is False
        assert result.roots == []

    def test_disc_connection_paths_appear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv(
            "OVID2_SETTINGS_FILE", str(tmp_path / "config" / "ovid2" / "settings.json")
        )
        monkeypatch.setenv(
            "OVID2_CONNECTIONS_DIR", str(tmp_path / "config" / "ovid2" / "connections")
        )
        host = tmp_path / "sandbox"
        host.mkdir()
        conns = tmp_path / "config" / "ovid2" / "connections"
        conns.mkdir(parents=True, exist_ok=True)
        (conns / "sandbox_disc.json").write_text(
            json.dumps({"type": "disc", "args": {"host_path": str(host)}}),
            encoding="utf-8",
        )
        _configure_settings(tmp_path, [str(tmp_path)])
        from ovid2.web.services import fs_svc

        result = fs_svc.list_roots()
        paths = [r.path for r in result.roots]
        assert str(host) in paths
