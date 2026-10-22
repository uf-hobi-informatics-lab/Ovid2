"""Phase 4 — storage-account browse service + endpoint tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_disc_connection(xdg: Path, name: str, host_path: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": host_path,
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        )
    )


@pytest.fixture()
def disc_tree(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "demo" / "20260101").mkdir(parents=True)
    (root / "demo" / "20260101" / "demographic.csv").write_text("x")
    (root / "acme").mkdir()
    return root


@pytest.mark.unit
def test_browse_root(web_client, isolated_xdg: Path, disc_tree: Path) -> None:  # type: ignore[no-untyped-def]
    _write_disc_connection(isolated_xdg, "local_disc", str(disc_tree))
    r = web_client.get("/api/v1/storage/local_disc/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["type"] == "disc"
    names = {e["name"] for e in body["entries"]}
    assert names == {"demo", "acme"}


@pytest.mark.unit
def test_browse_descends(web_client, isolated_xdg: Path, disc_tree: Path) -> None:  # type: ignore[no-untyped-def]
    _write_disc_connection(isolated_xdg, "local_disc", str(disc_tree))
    r = web_client.get(
        "/api/v1/storage/local_disc/browse", params={"prefix": "demo/20260101"}
    )
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert entries == [
        {"name": "demographic.csv", "kind": "file", "path": "demo/20260101/demographic.csv"}
    ]


@pytest.mark.unit
def test_browse_escape_is_reported_not_crash(
    web_client, isolated_xdg: Path, disc_tree: Path
) -> None:  # type: ignore[no-untyped-def]
    _write_disc_connection(isolated_xdg, "local_disc", str(disc_tree))
    r = web_client.get(
        "/api/v1/storage/local_disc/browse", params={"prefix": "../.."}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert "escapes" in (body["message"] or "")


@pytest.mark.unit
def test_browse_unknown_storage_404(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/api/v1/storage/nope/browse")
    assert r.status_code == 404


@pytest.mark.unit
def test_browse_unsupported_type_marks_supported_false(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """A postgres storage account has no list_dir → supported=False."""
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "pg.json").write_text(
        json.dumps(
            {
                "type": "postgres",
                "args": {
                    "host": "localhost",
                    "port": 5432,
                    "database": "d",
                    "user": "u",
                },
                "secret_ref": None,
            }
        )
    )
    r = web_client.get("/api/v1/storage/pg/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert "not available" in (body["message"] or "")
