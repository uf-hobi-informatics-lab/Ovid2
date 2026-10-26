"""Tests for :mod:`ovid2.dataspace.resolver` — the Data Space as plain directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.dataspace import list_children, list_storage_accounts, resolve
from ovid2.dataspace.path import OdsPath
from ovid2.dataspace.resolver import OdsResolutionError


@pytest.fixture()
def tree(isolated_xdg: Path, tmp_path: Path) -> Path:
    """A disc storage account ``inbox`` + one exact-location address."""
    root = tmp_path / "inbox_root"
    (root / "landing" / "2026_03").mkdir(parents=True)
    (root / "landing" / "2026_03" / "demographic.csv").write_text("a\n1\n")
    (root / "landing" / "2026_04").mkdir()
    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "inbox.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(root)}}), encoding="utf-8"
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "march.json").write_text(
        json.dumps({"connection": "inbox", "location": {"path": "landing/2026_03"}}),
        encoding="utf-8",
    )
    return root


class TestResolve:
    def test_root(self, tree: Path) -> None:
        target = resolve("/")
        assert target.connection is None

    def test_exact_location_and_matching_address(self, tree: Path) -> None:
        target = resolve("/inbox/landing/2026_03")
        assert target.connection == "inbox"
        assert target.type == "disc"
        assert target.location == "landing/2026_03"
        assert target.batch_id == "inbox/landing/2026_03"
        assert target.address_name == "march"
        assert target.candidates == ("march",)

    def test_case_insensitive_account(self, tree: Path) -> None:
        assert resolve("/INBOX/landing/2026_03").address_name == "march"

    def test_no_matching_address(self, tree: Path) -> None:
        target = resolve("/inbox/landing/2026_04")
        assert target.address_name is None
        assert target.batch_id == "inbox/landing/2026_04"

    def test_unknown_account_raises(self, tree: Path) -> None:
        with pytest.raises(OdsResolutionError):
            resolve("/nope/x")

    def test_path_object_accepted(self, tree: Path) -> None:
        assert resolve(OdsPath.parse("/inbox")).connection == "inbox"


class TestListing:
    def test_root_lists_storage_accounts(self, tree: Path) -> None:
        accounts = list_storage_accounts()
        assert [(a.name, a.kind, a.path, a.type) for a in accounts] == [
            ("inbox", "account", "/inbox", "disc")
        ]
        supported, entries, _ = list_children("/")
        assert supported and [e.name for e in entries] == ["inbox"]

    def test_folders_are_plain_directories(self, tree: Path) -> None:
        supported, entries, _ = list_children("/inbox/landing")
        assert supported
        assert [(e.name, e.kind, e.path) for e in entries] == [
            ("2026_03", "dir", "/inbox/landing/2026_03"),
            ("2026_04", "dir", "/inbox/landing/2026_04"),
        ]

    def test_leaf_lists_files(self, tree: Path) -> None:
        _, entries, _ = list_children("/inbox/landing/2026_03")
        assert [(e.name, e.kind) for e in entries] == [("demographic.csv", "file")]

    def test_custom_lister_is_used_for_every_store(self, tree: Path) -> None:
        calls: list[tuple[str, str | None]] = []

        def _lister(conn: str, prefix: str | None):  # type: ignore[no-untyped-def]
            calls.append((conn, prefix))
            return True, [{"name": "S1", "kind": "dir", "path": "DB/S1"}], None

        _, entries, _ = list_children("/inbox/DB", lister=_lister)
        assert calls == [("inbox", "DB")]
        assert entries[0].path == "/inbox/DB/S1"
