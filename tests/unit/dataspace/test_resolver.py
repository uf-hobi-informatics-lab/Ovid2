"""Tests for :mod:`ovid2.dataspace.resolver` — the Data Space as plain directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.dataspace import driver_lister, list_children, list_storage_accounts, resolve
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
        supported, entries, _ = list_children("/", lister=driver_lister)
        assert supported and [e.name for e in entries] == ["inbox"]

    def test_folders_are_plain_directories(self, tree: Path) -> None:
        supported, entries, _ = list_children("/inbox/landing", lister=driver_lister)
        assert supported
        assert [(e.name, e.kind, e.path) for e in entries] == [
            ("2026_03", "dir", "/inbox/landing/2026_03"),
            ("2026_04", "dir", "/inbox/landing/2026_04"),
        ]

    def test_leaf_lists_files(self, tree: Path) -> None:
        _, entries, _ = list_children("/inbox/landing/2026_03", lister=driver_lister)
        assert [(e.name, e.kind) for e in entries] == [("demographic.csv", "file")]

    def test_custom_lister_is_used_for_every_store(self, tree: Path) -> None:
        calls: list[tuple[str, str | None]] = []

        def _lister(conn: str, prefix: str | None):  # type: ignore[no-untyped-def]
            calls.append((conn, prefix))
            return True, [{"name": "S1", "kind": "dir", "path": "DB/S1"}], None

        _, entries, _ = list_children("/inbox/DB", lister=_lister)
        assert calls == [("inbox", "DB")]
        assert entries[0].path == "/inbox/DB/S1"


class TestUnknownAccountSuggestions:
    @pytest.fixture()
    def archive(self, isolated_xdg: Path, tmp_path: Path) -> Path:
        conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
        (conns / "inboxarchive_acme.json").write_text(
            json.dumps({"type": "disc", "args": {"host_path": "/data/inboxarchive/ACME"}}),
            encoding="utf-8",
        )
        (conns / "sf.json").write_text(
            json.dumps({"type": "snowflake", "args": {"account": "a"}}), encoding="utf-8"
        )
        return conns

    def test_close_names_and_sibling_proposal(self, archive: Path) -> None:
        from ovid2.dataspace.resolver import suggest_accounts

        close, proposals = suggest_accounts("/inboxarchive/NU")
        assert close == ["inboxarchive_acme"]
        assert [(p.name, p.type, p.host_path, p.sibling_of) for p in proposals] == [
            ("inboxarchive_nu", "disc", "/data/inboxarchive/NU", "inboxarchive_acme")
        ]

    def test_error_message_carries_suggestions(self, archive: Path) -> None:
        with pytest.raises(OdsResolutionError) as exc:
            resolve("/inboxarchive/NU")
        msg = str(exc.value)
        assert msg.startswith("Storage account 'inboxarchive' does not exist.")
        assert "inboxarchive_acme" in msg
        assert "'inboxarchive_nu' with host_path '/data/inboxarchive/NU'" in msg

    def test_no_proposal_when_name_already_taken(self, archive: Path) -> None:
        from ovid2.dataspace.resolver import suggest_accounts

        (archive / "inboxarchive_nu.json").write_text(
            json.dumps({"type": "disc", "args": {"host_path": "/data/inboxarchive/NU"}}),
            encoding="utf-8",
        )
        _close, proposals = suggest_accounts("/inboxarchive/NU")
        assert proposals == []

    def test_unrelated_name_has_no_suggestions(self, archive: Path) -> None:
        from ovid2.dataspace.resolver import suggest_accounts

        assert suggest_accounts("/zzz/q") == ([], [])
