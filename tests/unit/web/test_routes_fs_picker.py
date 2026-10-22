"""Route tests for the FS picker (``/api/v1/fs/*`` + ``/fs/picker``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _configure_picker(
    isolated_xdg: Path, roots: list[str], enabled: bool = True
) -> None:
    settings_path = isolated_xdg / "config" / "ovid2" / "settings.json"
    settings_path.write_text(
        json.dumps({"fs_picker": {"enabled": enabled, "roots": roots}}),
        encoding="utf-8",
    )
    from ovid2.web.services import settings_svc
    settings_svc.invalidate_cache()


class TestFsListEndpoint:
    def test_list_returns_directory(self, isolated_xdg: Path, web_client) -> None:
        target = isolated_xdg / "target"
        target.mkdir()
        (target / "child").mkdir()
        _configure_picker(isolated_xdg, [str(target)])
        response = web_client.get("/api/v1/fs/list", params={"path": str(target)})
        assert response.status_code == 200
        body = response.json()
        assert body["path"] == str(target)
        names = [e["name"] for e in body["entries"]]
        assert "child" in names

    def test_list_outside_allowlist_returns_403(
        self, isolated_xdg: Path, web_client
    ) -> None:
        allowed = isolated_xdg / "allowed"
        allowed.mkdir()
        outside = isolated_xdg / "outside"
        outside.mkdir()
        _configure_picker(isolated_xdg, [str(allowed)])
        response = web_client.get("/api/v1/fs/list", params={"path": str(outside)})
        assert response.status_code == 403

    def test_list_missing_path_returns_404(
        self, isolated_xdg: Path, web_client
    ) -> None:
        _configure_picker(isolated_xdg, [str(isolated_xdg)])
        response = web_client.get(
            "/api/v1/fs/list", params={"path": str(isolated_xdg / "nope")}
        )
        assert response.status_code == 404

    def test_list_disabled_returns_404(
        self, isolated_xdg: Path, web_client
    ) -> None:
        _configure_picker(isolated_xdg, ["/"], enabled=False)
        response = web_client.get("/api/v1/fs/list", params={"path": "/"})
        assert response.status_code == 404


class TestFsRootsEndpoint:
    def test_returns_curated_roots(self, isolated_xdg: Path, web_client) -> None:
        _configure_picker(isolated_xdg, [str(isolated_xdg)])
        response = web_client.get("/api/v1/fs/roots")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        paths = [r["path"] for r in body["roots"]]
        assert str(isolated_xdg) in paths

    def test_disabled(self, isolated_xdg: Path, web_client) -> None:
        _configure_picker(isolated_xdg, ["/"], enabled=False)
        response = web_client.get("/api/v1/fs/roots")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["roots"] == []


class TestFsPickerPartial:
    def test_picker_partial_renders(self, isolated_xdg: Path, web_client) -> None:
        target = isolated_xdg / "target"
        target.mkdir()
        (target / "kid").mkdir()
        _configure_picker(isolated_xdg, [str(target)])
        response = web_client.get(
            "/fs/picker",
            params={"target": "arg_host_path", "path": str(target)},
        )
        assert response.status_code == 200
        assert "Choose this directory" in response.text
        assert "kid" in response.text

    def test_picker_partial_shows_error_on_403(
        self, isolated_xdg: Path, web_client
    ) -> None:
        allowed = isolated_xdg / "allowed"
        allowed.mkdir()
        outside = isolated_xdg / "outside"
        outside.mkdir()
        _configure_picker(isolated_xdg, [str(allowed)])
        response = web_client.get(
            "/fs/picker",
            params={"target": "arg_host_path", "path": str(outside)},
        )
        assert response.status_code == 200  # partial renders inline error
        assert "outside" in response.text.lower() or "allowlist" in response.text.lower()
