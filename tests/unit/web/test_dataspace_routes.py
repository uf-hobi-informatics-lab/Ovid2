"""Tests for the Data Space web routes — every store is a tree of directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.models import StorageBrowseEntry, StorageBrowseResponse


@pytest.fixture()
def disc_account(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disc storage account whose browse is answered in-process (no broker)."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "disc"
    (root / "landing" / "2026_03").mkdir(parents=True)
    (root / "landing" / "2026_03" / "DEMOGRAPHIC.csv").write_text("a\n")
    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "inbox.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(root)}}), encoding="utf-8"
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "march.json").write_text(
        json.dumps({"connection": "inbox", "location": {"path": "landing/2026_03"}}),
        encoding="utf-8",
    )

    from ovid2.drivers.disc.driver import DiscDriver
    from ovid2.web.services import storage_browse_svc

    def _browse(name: str, prefix: str | None = None) -> StorageBrowseResponse:
        driver = DiscDriver(args={"host_path": str(root)}, secrets={}, location={})
        entries = [
            StorageBrowseEntry(
                name=e.name,
                kind=e.kind,
                path=e.path,
                size_bytes=e.size_bytes,
                modified_at=e.modified_at,
            )
            for e in driver.list_dir(prefix)
        ]
        return StorageBrowseResponse(
            storage=name, type="disc", prefix=prefix or "", entries=entries
        )

    monkeypatch.setattr(storage_browse_svc, "browse_storage", _browse)
    return root


class TestList:
    def test_root_lists_storage_accounts(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace")
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "/"
        assert [(e["name"], e["kind"]) for e in body["entries"]] == [("inbox", "account")]

    def test_folder_listing(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace", params={"path": "/inbox/landing"})
        assert r.status_code == 200
        body = r.json()
        assert body["connection"] == "inbox"
        assert body["batch_id"] == "inbox/landing"
        assert [(e["name"], e["path"]) for e in body["entries"]] == [
            ("2026_03", "/inbox/landing/2026_03")
        ]

    def test_leaf_reports_matching_address(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace", params={"path": "/inbox/landing/2026_03"})
        body = r.json()
        assert body["address_name"] == "march"
        assert [e["name"] for e in body["entries"]] == ["DEMOGRAPHIC.csv"]

    def test_bad_path_400(self, web_client, disc_account) -> None:
        assert web_client.get("/api/v1/dataspace", params={"path": "/a/../b"}).status_code == 400

    def test_unknown_account_400(self, web_client, disc_account) -> None:
        assert web_client.get("/api/v1/dataspace", params={"path": "/nope"}).status_code == 400


class TestDimensions:
    def test_returns_storage_types(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace/dimensions")
        assert r.status_code == 200
        names = {d["name"] for d in r.json()["dimensions"]}
        assert {"disc", "snowflake", "s3", "azure_blob"}.issubset(names)


class TestResolve:
    def test_happy_path(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace/resolve", params={"path": "/inbox/landing/2026_03"})
        assert r.status_code == 200
        body = r.json()
        assert body["connection"] == "inbox"
        assert body["location"] == "landing/2026_03"
        assert body["batch_id"] == "inbox/landing/2026_03"
        assert body["address_name"] == "march"

    def test_bad_path_returns_400(self, web_client, disc_account) -> None:
        r = web_client.get("/api/v1/dataspace/resolve", params={"path": "not_absolute"})
        assert r.status_code == 400


class TestHtml:
    def test_root_and_folder_pages_render(self, web_client, disc_account) -> None:
        r = web_client.get("/dataspace")
        assert r.status_code == 200
        assert "/inbox" in r.text  # account in the boot JSON + <noscript> list
        assert "dataspace_explorer.js" in r.text
        r = web_client.get("/dataspace/inbox/landing/2026_03")
        assert r.status_code == 200
        # The explorer opens at the deep-linked path (crumbs + boot JSON).
        assert '"path": "/inbox/landing/2026_03"' in r.text
        assert 'data-ds-nav="/inbox/landing/2026_03"' in r.text

    def test_table_deep_link_renders_shell(self, web_client, disc_account) -> None:
        r = web_client.get("/dataspace/inbox/landing/2026_03/DEMOGRAPHIC.csv")
        assert r.status_code == 200
        assert "data-ds-export-modal" in r.text

    def test_bad_path_400(self, web_client, disc_account) -> None:
        assert web_client.get("/dataspace/a/@b").status_code == 400
